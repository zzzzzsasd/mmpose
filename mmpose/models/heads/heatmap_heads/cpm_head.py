# Copyright (c) OpenMMLab. All rights reserved.
from typing import List, Optional, Sequence, Union

import torch
from mmcv.cnn import build_conv_layer, build_upsample_layer
from mmengine.data import PixelData
from torch import Tensor, nn

from mmpose.evaluation.functional import pose_pck_accuracy
from mmpose.registry import KEYPOINT_CODECS, MODELS
from mmpose.utils.tensor_utils import to_numpy
from mmpose.utils.typing import (MultiConfig, OptConfigType, OptSampleList,
                                 SampleList)
from ..base_head import BaseHead

OptIntSeq = Optional[Sequence[int]]


@MODELS.register_module()
class CPMHead(BaseHead):
    """Multi-stage heatmap head introduced in `Convolutional Pose Machines`_ by
    Wei et al (2016) and used by `Stacked Hourglass Networks`_ by Newell et al
    (2016). The head consists of multiple branches, each of which has some
    deconv layers and a simple conv2d layer.

    Args:
        in_channels (int | Sequence[int]): Number of channels in the input
            feature maps.
        out_channels (int): Number of channels in the output heatmaps.
        num_stages (int): Number of stages.
        deconv_out_channels (Sequence[int], optional): The output channel
            number of each deconv layer. Defaults to ``(256, 256, 256)``
        deconv_kernel_sizes (Sequence[int | tuple], optional): The kernel size
            of each deconv layer. Each element should be either an integer for
            both height and width dimensions, or a tuple of two integers for
            the height and the width dimension respectively.
            Defaults to ``(4, 4, 4)``
        has_final_layer (bool): Whether have the final 1x1 Conv2d layer.
            Defaults to ``True``
        loss (Config | List[Config]): Config of the keypoint loss of different
            stages. Defaults to use :class:`KeypointMSELoss`.
        decoder (Config, optional): The decoder config that controls decoding
            keypoint coordinates from the network output. Defaults to ``None``
        init_cfg (Config, optional): Config to control the initialization. See
            :attr:`default_init_cfg` for default settings

    .. _`Convolutional Pose Machines`: https://arxiv.org/abs/1602.00134
    .. _`Stacked Hourglass Networks`: https://arxiv.org/abs/1603.06937
    """

    _version = 2

    def __init__(self,
                 in_channels: Union[int, Sequence[int]],
                 out_channels: int,
                 num_stages: int,
                 deconv_out_channels: OptIntSeq = None,
                 deconv_kernel_sizes: OptIntSeq = None,
                 has_final_layer: bool = True,
                 loss: MultiConfig = dict(
                     type='KeypointMSELoss', use_target_weight=True),
                 decoder: OptConfigType = None,
                 init_cfg: OptConfigType = None):

        if init_cfg is None:
            init_cfg = self.default_init_cfg
        super().__init__(init_cfg)

        self.num_stages = num_stages
        self.in_channels = in_channels
        self.out_channels = out_channels

        if isinstance(loss, list) and len(loss) != num_stages:
            raise ValueError(
                f'The length of loss_module({len(loss)}) did not match '
                f'`num_stages`({num_stages})')

        self.loss_module = MODELS.build(loss)

        if decoder is not None:
            self.decoder = KEYPOINT_CODECS.build(decoder)
        else:
            self.decoder = None

        # build multi-stage deconv layers
        self.multi_deconv_layers = nn.ModuleList([])
        if deconv_out_channels:
            if deconv_kernel_sizes is None or len(deconv_out_channels) != len(
                    deconv_kernel_sizes):
                raise ValueError(
                    '"deconv_out_channels" and "deconv_kernel_sizes" should '
                    'be integer sequences with the same length. Got '
                    f'unmatched values {deconv_out_channels} and '
                    f'{deconv_kernel_sizes}')

            for _ in range(self.num_stages):
                deconv_layers = self._make_deconv_layers(
                    in_channels=in_channels,
                    layer_out_channels=deconv_out_channels,
                    layer_kernel_sizes=deconv_kernel_sizes,
                )
                self.multi_deconv_layers.append(deconv_layers)
            in_channels = deconv_out_channels[-1]
        else:
            for _ in range(self.num_stages):
                self.multi_deconv_layers.append(nn.Identity())

        # build multi-stage final layers
        self.multi_final_layers = nn.ModuleList([])
        if has_final_layer:
            cfg = dict(
                type='Conv2d',
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=1)
            for _ in range(self.num_stages):
                self.multi_final_layers.append(build_conv_layer(cfg))
        else:
            for _ in range(self.num_stages):
                self.multi_final_layers.append(nn.Identity())

    @property
    def default_init_cfg(self):
        init_cfg = [
            dict(
                type='Normal', layer=['Conv2d', 'ConvTranspose2d'], std=0.001),
            dict(type='Constant', layer='BatchNorm2d', val=1)
        ]
        return init_cfg

    def _make_deconv_layers(self, in_channels: int,
                            layer_out_channels: Sequence[int],
                            layer_kernel_sizes: Sequence[int]) -> nn.Module:
        """Create deconvolutional layers by given parameters."""

        layers = []
        for out_channels, kernel_size in zip(layer_out_channels,
                                             layer_kernel_sizes):
            if kernel_size == 4:
                padding = 1
                output_padding = 0
            elif kernel_size == 3:
                padding = 1
                output_padding = 1
            elif kernel_size == 2:
                padding = 0
                output_padding = 0
            else:
                raise ValueError(f'Unsupported kernel size {kernel_size} for'
                                 'deconvlutional layers in '
                                 f'{self.__class__.__name__}')
            cfg = dict(
                type='deconv',
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                stride=2,
                padding=padding,
                output_padding=output_padding,
                bias=False)
            layers.append(build_upsample_layer(cfg))
            layers.append(nn.BatchNorm2d(num_features=out_channels))
            layers.append(nn.ReLU(inplace=True))
            in_channels = out_channels

        return nn.Sequential(*layers)

    def forward(self, feats: Sequence[Tensor]) -> List[Tensor]:
        """Forward the network. The input is multi-stage feature maps and the
        output is a list of heatmaps from multiple stages.

        Args:
            feats (Sequence[Tensor]): Multi-stage feature maps.

        Returns:
            List[Tensor]: A list of output heatmaps from multiple stages.
        """
        out = []
        assert len(feats) == self.num_stages, (
            f'The length of feature maps did not match the '
            f'`num_stages` in {self.__class__.__name__}')
        for i in range(self.num_stages):
            y = self.multi_deconv_layers[i](feats[i])
            y = self.multi_final_layers[i](y)
            out.append(y)

        return out

    def predict(self,
                feats: Sequence[Tensor],
                batch_data_samples: OptSampleList,
                test_cfg: OptConfigType = {}) -> SampleList:
        """Predict results from multi-stage feature maps.

        Args:
            feats (Sequence[Tensor]): Multi-stage feature maps.
            batch_data_samples (List[:obj:`PoseDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instances`.
            test_cfg (Config, optional): The testing/inference config.

        Returns:
            List[:obj:`PoseDataSample`]: Pose estimation results of each
            sample after the post process.
        """
        multi_stage_batch_heatmaps = self.forward(feats)
        batch_heatmaps = multi_stage_batch_heatmaps[-1]

        preds = self.decode(batch_heatmaps, batch_data_samples)

        # Whether to visualize the predicted heatmaps
        if test_cfg.get('output_heatmaps', False):
            for heatmaps, data_sample in zip(batch_heatmaps, preds):
                # Store the heatmap predictions in the data sample
                if 'pred_fileds' not in data_sample:
                    data_sample.pred_fields = PixelData()
                data_sample.pred_fields.heatmaps = heatmaps

        return preds

    def loss(self,
             feats: Sequence[Tensor],
             batch_data_samples: OptSampleList,
             train_cfg: OptConfigType = {}) -> dict:
        """Calculate losses from a batch of inputs and data samples.

        Args:
            feats (Sequence[Tensor]): Multi-stage feature maps.
            batch_data_samples (List[:obj:`PoseDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instances`.
            train_cfg (Config, optional): The training config.

        Returns:
            dict: A dictionary of loss components.
        """
        multi_stage_pred_heatmaps = self.forward(feats)

        gt_heatmaps = torch.stack(
            [d.gt_fields.heatmaps for d in batch_data_samples])
        keypoint_weights = torch.cat([
            d.gt_instance_labels.keypoint_weights for d in batch_data_samples
        ])

        # calculate losses over multiple stages
        losses = dict()
        for i in range(self.num_stages):
            if isinstance(self.loss_module, nn.Sequential):
                # use different loss_module over different stages
                loss_func = self.loss_module[i]
            else:
                # use the same loss_module over different stages
                loss_func = self.loss_module

            # the `gt_heatmaps` and `keypoint_weights` used to calculate loss
            # for different stages are the same
            loss_i = loss_func(multi_stage_pred_heatmaps[i], gt_heatmaps,
                               keypoint_weights)

            if 'loss_kpt' not in losses:
                losses['loss_kpt'] = loss_i
            else:
                losses['loss_kpt'] += loss_i

        # calculate accuracy
        _, avg_acc, _ = pose_pck_accuracy(
            output=to_numpy(multi_stage_pred_heatmaps[-1]),
            target=to_numpy(gt_heatmaps),
            mask=to_numpy(keypoint_weights) > 0)

        losses.update(acc_pose=float(avg_acc))

        return losses
