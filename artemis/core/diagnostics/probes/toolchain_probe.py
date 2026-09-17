# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Toolchain and Video Recording Auxiliary Probe."""

from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
from artemis.platform import OSType, platform
from artemis.toolchain import toolchain


class ToolchainProbe(BaseProbe):
    """Probe checking required multimedia and video recording toolchains (ffmpeg, scrcpy)."""

    @property
    def probe_id(self) -> str:
        return "toolchain"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.TOOLCHAIN

    @property
    def is_blocker(self) -> bool:
        return False

    async def probe(self) -> ProbeResult:
        ffmpeg_path = toolchain.resolve("ffmpeg")
        scrcpy_path = toolchain.resolve("scrcpy")

        tools_installed = []
        tools_missing = []
        if ffmpeg_path:
            tools_installed.append("FFmpeg")
        else:
            tools_missing.append("FFmpeg")

        if scrcpy_path:
            tools_installed.append("scrcpy")
        else:
            tools_missing.append("scrcpy")

        metadata = {
            "ffmpeg": ffmpeg_path is not None,
            "scrcpy": scrcpy_path is not None,
            "ffmpeg_path": ffmpeg_path,
            "scrcpy_path": scrcpy_path,
            "tools_installed": tools_installed,
            "tools_missing": tools_missing,
        }

        if ffmpeg_path and scrcpy_path:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="视频录制工具链",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="就绪（FFmpeg + scrcpy）",
                description=f"FFmpeg（{ffmpeg_path}）与 scrcpy（{scrcpy_path}）均可用。实时投屏与高速视频回放已启用。",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="工具链可用",
                        payload="FFmpeg 与 scrcpy 已安装并可用。",
                    )
                ],
            )

        missing_str = " & ".join(tools_missing)
        actions = []

        if platform.os_type == OSType.WINDOWS:
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="通过 WinGet 安装",
                    payload="winget install Gyan.FFmpeg Genymobile.scrcpy",
                )
            )
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="运行 PowerShell 安装脚本",
                    payload="powershell -ExecutionPolicy Bypass -File scripts/install_deps.ps1",
                )
            )
        elif platform.os_type == OSType.MACOS:
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="通过 Homebrew 安装",
                    payload="brew install ffmpeg scrcpy",
                )
            )
        else:
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="在 Linux 上安装",
                    payload="sudo apt-get install -y ffmpeg scrcpy",
                )
            )
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="一键安装脚本",
                    payload="bash scripts/install_deps.sh",
                )
            )

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="视频录制工具链",
            status=ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary=f"缺少 {missing_str}",
            description=f"屏幕投屏与测试回放录制需要视频工具链。当前缺少：{missing_str}。",
            metadata=metadata,
            actions=actions,
        )
