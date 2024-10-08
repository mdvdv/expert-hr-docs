from __future__ import annotations

from typing import Tuple

import torch
import torch.nn.functional as F
import torch.nn.init as init
from torch import Tensor, nn
from torchvision import models

from apps.expert.core.functional_tools import get_model_weights


class DAN(nn.Module):
    """Distract Your Attention Network implementation.

    Distract Your Attention Network performs facial
    expression recognition on tensor face images.
    """

    def __init__(
        self,
        num_class: int = 8,
        num_head: int = 4,
        pretrained: bool = True,
        device: torch.device | None = None,
    ) -> None:
        """
        Args:
            num_class (int, optional): Number of model output classes. Defaults to 8.
            num_head (int, optional): Number of heads in multihead classification. Defaults to 4.
            pretrained (bool, optional): Whether or not to load saved pretrained weights. Defaults to True.
            device (torch.device | None, optional): Device type on local machine (GPU recommended). Defaults to None.
        """
        super(DAN, self).__init__()

        resnet = models.resnet18(pretrained=False)

        self.features = nn.Sequential(*list(resnet.children())[:-2])
        self.num_head = num_head

        for idx in range(num_head):
            setattr(self, "cat_head{}".format(idx), CrossAttentionHead())

        self.sig = nn.Sigmoid()
        self.fc = nn.Linear(512, num_class)
        self.bn = nn.BatchNorm1d(num_class)
        self._device = torch.device("cpu")

        if pretrained:
            url = "https://drive.google.com/uc?export=view&id=1uHNADViICyJEjJljv747nfvrGu12kjtu"
            model_name = "affecnet8_epoch5_acc0.6209.pth"
            cached_file = get_model_weights(model_name=model_name, url=url)
            state_dict = torch.load(cached_file, map_location=self._device)
            self.load_state_dict(state_dict["model_state_dict"], strict=True)

        if device is not None:
            self._device = device
            self.to(self._device)

    @property
    def device(self) -> torch.device:
        """Check the device type.

        Returns:
            torch.device: Device type on local machine.
        """
        return self._device

    def forward(self, x: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        x = self.features(x)
        heads = []

        for idx in range(self.num_head):
            heads.append(getattr(self, "cat_head{}".format(idx))(x))

        heads = torch.stack(heads).permute([1, 0, 2])

        if heads.size(1) > 1:
            heads = F.log_softmax(heads, dim=1)

        out = self.fc(heads.sum(dim=1))
        out = self.bn(out)

        return out, x, heads


class CrossAttentionHead(nn.Module):
    def __init__(self) -> None:
        super().__init__()

        self.sa = SpatialAttention()
        self.ca = ChannelAttention()
        self.init_weights()

    def init_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, mode="fan_out")
                if m.bias is not None:
                    init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                init.constant_(m.weight, 1)
                init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                init.normal_(m.weight, std=0.001)
                if m.bias is not None:
                    init.constant_(m.bias, 0)

    def forward(self, x: Tensor) -> Tensor:
        sa = self.sa(x)
        ca = self.ca(sa)

        return ca


class SpatialAttention(nn.Module):
    def __init__(self) -> None:
        super().__init__()

        self.conv1x1 = nn.Sequential(
            nn.Conv2d(512, 256, kernel_size=1),
            nn.BatchNorm2d(256),
        )

        self.conv_3x3 = nn.Sequential(
            nn.Conv2d(256, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
        )

        self.conv_1x3 = nn.Sequential(
            nn.Conv2d(256, 512, kernel_size=(1, 3), padding=(0, 1)),
            nn.BatchNorm2d(512),
        )

        self.conv_3x1 = nn.Sequential(
            nn.Conv2d(256, 512, kernel_size=(3, 1), padding=(1, 0)),
            nn.BatchNorm2d(512),
        )

        self.relu = nn.ReLU()

    def forward(self, x: Tensor) -> Tensor:
        y = self.conv1x1(x)
        y = self.relu(self.conv_3x3(y) + self.conv_1x3(y) + self.conv_3x1(y))
        y = y.sum(dim=1, keepdim=True)
        out = x * y

        return out


class ChannelAttention(nn.Module):
    def __init__(self) -> None:
        super().__init__()

        self.gap = nn.AdaptiveAvgPool2d(1)

        self.attention = nn.Sequential(
            nn.Linear(512, 32),
            nn.BatchNorm1d(32),
            nn.ReLU(inplace=True),
            nn.Linear(32, 512),
            nn.Sigmoid(),
        )

    def forward(self, sa: Tensor) -> Tensor:
        sa = self.gap(sa)
        sa = sa.view(sa.size(0), -1)
        y = self.attention(sa)
        out = sa * y

        return out
