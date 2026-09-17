/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Launcher options for `/api/run` (`verification_level`, `explorer_mode`).
 * Keep ids in sync with `VERIFICATION_LEVEL_PRESETS` in `artemis/config/agent.py`
 * and `EXPLORER_TIERS` in `artemis/agents/explorer/tiers.py`.
 */

export type VerificationLevelId = 'off' | 'final' | 'checkpoints' | 'strict';
export type ExplorerModeId = 'flash' | 'pro' | 'ultra';

/** One notch on a tuning slider. */
export interface TuningLevel<TId extends string = string> {
  /** Wire value sent to the backend. */
  id: TId;
  /** Short name shown next to the slider title and as the hover card heading. */
  label: string;
  /** One-sentence summary shown in the hover card. */
  tagline: string;
  /** Plain-language time cost, e.g. "no extra time". */
  latency: string;
  /** Checks or searches performed at this level. */
  runs: string[];
  /** Checks or searches omitted at this level. */
  skips?: string[];
  /** When to pick this level. */
  bestFor: string;
}

export const VERIFICATION_LEVELS: readonly TuningLevel<VerificationLevelId>[] = [
  {
    id: 'off',
    label: '关闭',
    tagline: '不做任何校验。任务看起来完成就立即结束。',
    latency: '不额外耗时',
    runs: [
      '每一步只要执行完就视为完成。',
      '你能拿到完整的动作轨迹，但没有通过 / 失败的判定。'
    ],
    skips: ['不会做二次核对，也不会重试。'],
    bestFor: '快速尝试和演示，只想观察运行过程时使用。'
  },
  {
    id: 'final',
    label: '结束时校验',
    tagline: '在任务结束时，用最终结果对照目标做一次校验。这是默认档位。',
    latency: '结束时增加约 20–60 秒',
    runs: [
      '任务结束时，会把最终屏幕、步骤历史与设备状态同你的要求进行比对。',
      '如果结果不符，任务会回到前面尝试修复，最多 3 次。'
    ],
    skips: ['任务运行过程中不做任何校验。'],
    bestFor: '日常任务：给出可信的通过 / 失败判定，又不拖慢运行。'
  },
  {
    id: 'checkpoints',
    label: '每步校验',
    tagline: '每一步完成后立即校验，并在结束时再做一次总校验。',
    latency: '每步后做一次简短校验，在后台完成',
    runs: [
      '每一步完成后立即用当时的截图进行校验。',
      '某一步出错时，会先修复再继续（每步最多重试 2 次）。',
      '失败的校验条件会被记录下来，任务继续执行。',
      '结束时仍会执行总校验。'
    ],
    bestFor: '长流程任务，尤其是早期一步出错就会影响后续全部的场景。'
  },
  {
    id: 'strict',
    label: '严格',
    tagline: '每一步都校验，并增加重试次数。首个失败的校验会中止运行。',
    latency: '最慢：校验更多、重试更多',
    runs: [
      '每次校验耗时更长、尝试次数更多：每步 4 次修复，结束时 5 次。',
      '首个失败的校验条件会立即中止运行，并附上相关证据。'
    ],
    bestFor: '发版检查与回归测试，绝不允许误判为通过的场景。'
  }
];

export const EXPLORER_MODES: readonly TuningLevel<ExplorerModeId>[] = [
  {
    id: 'flash',
    label: '快速一瞥',
    tagline: '一次观察即可在屏幕上找到按钮和文字。',
    latency: '每次查找观察 1 次',
    runs: [
      '按名称、图标或颜色在屏幕上查找目标，立即返回其位置。',
      '可以同时查找多个目标。'
    ],
    skips: ['不放大，也不做二次尝试。'],
    bestFor: '按钮、图标和文字标注清晰的普通应用。'
  },
  {
    id: 'pro',
    label: '二次确认',
    tagline: '最多观察 3 次，中间会进行推理，然后给出答案。',
    latency: '每次查找最多观察 3 次',
    runs: [
      '先读取屏幕布局，再对图像进行查找。',
      '如果首次未命中，会在 3 次观察内换一种方式再试。'
    ],
    skips: ['仍不会放大小区域，以保持查找速度。'],
    bestFor: '按位置描述的目标（如“Wi-Fi 旁边的开关”），或标注不清晰的元素。'
  },
  {
    id: 'ultra',
    label: '近距离细看',
    tagline: '会放大屏幕局部，最多观察 8 次。',
    latency: '每次查找最多观察 8 次（最慢）',
    runs: [
      '可以裁剪并放大屏幕局部，逐块读取小字和密集布局。',
      '后续观察会复用前面的结果，因此耗时没有听起来那么长。'
    ],
    bestFor: '界面拥挤、目标极小、图表与手绘内容，以及位置必须精确的场景。'
  }
];

/** Per-run tuning sent with `/api/run` for the Pro profile. */
export interface ProTuningOptions {
  verificationLevel?: VerificationLevelId | string;
  explorerMode?: ExplorerModeId | string;
}

/** Effective defaults reported by `GET /api/run/defaults`. */
export interface ProTuningDefaults {
  verification_level?: string | null;
  explorer_mode?: string | null;
}

export const DEFAULT_VERIFICATION_LEVEL: VerificationLevelId = 'final';
export const DEFAULT_EXPLORER_MODE: ExplorerModeId = 'flash';

/** Index of a level id within its ladder; falls back to the default when unknown. */
export function levelIndex<TId extends string>(
  ladder: readonly TuningLevel<TId>[],
  id: string | null | undefined,
  fallback: TId
): number {
  const wanted = String(id ?? '').trim().toLowerCase();
  const idx = ladder.findIndex((l) => l.id === wanted);
  if (idx >= 0) return idx;
  return Math.max(0, ladder.findIndex((l) => l.id === fallback));
}

/** Slider fill percentage for a notch index on a ladder of `count` notches. */
export function notchPercent(index: number, count: number): number {
  if (count <= 1) return 0;
  const clamped = Math.min(Math.max(index, 0), count - 1);
  return (clamped / (count - 1)) * 100;
}
