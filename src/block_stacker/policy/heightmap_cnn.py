"""Small CNN for the 4-channel heightmap observation stream.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    ハイトマップ観測 (4ch × 32×32) を固定長ベクトルに圧縮。設計書 §4 の
    観測 2 ストリーム目を担当。

設計上のポイント:
    - 構造: stride 2 の Conv 3 段 (4→16→32→64ch) → AdaptiveAvgPool(1) → Linear。
      → 32×32 → 16×16 → 8×8 → 4×4 → 1×1。十分シンプル。
    - AdaptiveAvgPool でグローバル集約 → 入力解像度依存をなくす。
      解像度を変えても fc 層を再設計しなくて良い設計。

レビューで見る観点:
    - in_channels=4 は (height, dz/dx, dz/dy, |grad|) のチャネル数と対応。
"""
from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class HeightmapCNN(nn.Module):
    """4×32×32 → output_dim. 3 strided convs + adaptive pool + linear."""

    def __init__(self, in_channels: int = 4, output_dim: int = 32) -> None:
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, 16, kernel_size=3, stride=2, padding=1),  # 16x16
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),  # 8x8
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1),  # 4x4
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(),
        )
        self.proj = nn.Linear(64, output_dim)

    def forward(self, x: Tensor) -> Tensor:
        return self.proj(self.conv(x))
