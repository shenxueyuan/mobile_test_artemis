import {
  cleanErrorMessage,
  extractToolExtraParams,
  getCompressionLabel,
  getCompressionPhase,
  getCompressionPhaseLabel,
  isCompressionWaiting,
  getToolDisplayLabel,
  getToolIcon,
  getToolTargetText,
  getUniqueGenericTools,
  getVideoAnalysisView,
  joinTargetDescriptions,
  shouldShowTool
} from './tool-formatter.util';

describe('compress_history timeline line', () => {
  const trace = (status: string, args: Record<string, any>) => ({
    type: 'tool',
    name: 'compress_history',
    status,
    payload: { args }
  });

  it('is shown as a plain tool line with its own icon', () => {
    const running = trace('running', { start_step: 12, end_step: 27 });
    expect(shouldShowTool(running)).toBeTrue();
    expect(getToolIcon(running)).toBe('compress');
    expect(getToolDisplayLabel(running)).toBe(getCompressionLabel(running));
  });

  it('says which steps are being condensed while running', () => {
    expect(getCompressionLabel(trace('running', { start_step: 12, end_step: 27 })))
      .toBe('正在把 步骤 12–27 压缩为简要记忆以释放空间…');
    expect(getCompressionLabel(trace('running', { start_step: 12, end_step: 27, note: 'retrying' })))
      .toBe('正在重试 步骤 12–27 的记忆摘要…');
    expect(getCompressionLabel(trace('running', { start_step: 5, end_step: 5 })))
      .toBe('正在把 步骤 5 压缩为简要记忆以释放空间…');
  });

  it('says the summary is ready but held while working memory is still small', () => {
    const held = trace('running', {
      start_step: 1,
      end_step: 4,
      note: 'held',
      context_tokens: 20000,
      context_budget: 80000,
      swap_at_tokens: 28000
    });
    expect(getCompressionLabel(held)).toBe(
      '步骤 1–4 的简要记忆已就绪；在工作记忆占满前保留完整记录 · ≈ 工作记忆 20k / 28k tokens'
    );
    expect(getCompressionLabel(trace('running', { start_step: 1, end_step: 4, note: 'held' })))
      .toBe('步骤 1–4 的简要记忆已就绪；在工作记忆占满前保留完整记录');
  });

  it('reports the size reduction and the working memory once done', () => {
    const done = trace('success', {
      start_step: 12,
      end_step: 27,
      source_tokens: 8400,
      summary_tokens: 600,
      context_tokens: 54000,
      context_budget: 80000
    });
    expect(getCompressionLabel(done)).toBe(
      '步骤 12–27 已压缩为简要记忆 · 8.4k → 600 tokens（缩小 14 倍） · 工作记忆 ≈ 54k / 80k tokens'
    );
  });

  it('omits the working memory figure on lines that do not carry it', () => {
    const done = trace('success', { start_step: 1, end_step: 4, source_tokens: 3000, summary_tokens: 1500 });
    expect(getCompressionLabel(done)).toBe('步骤 1–4 已压缩为简要记忆 · 3k → 1.5k tokens（缩小 2 倍）');
  });

  it('explains a forced recap and a failed attempt in plain words', () => {
    const forced = trace('success', { start_step: 1, end_step: 4, forced: true, context_tokens: 70000, context_budget: 80000 });
    expect(getCompressionLabel(forced))
      .toBe('步骤 1–4 已压缩为摘要以释放记忆 · 工作记忆 ≈ 70k / 80k tokens');
    expect(getCompressionLabel(trace('failed', { start_step: 1, end_step: 4 })))
      .toBe('暂时无法压缩 步骤 1–4；保留完整记录，稍后重试');
  });

  describe('phase (args.phase from the backend)', () => {
    it('reads the declared phase and maps it to one short plain-language label', () => {
      const cases: Array<[string, string, string]> = [
        ['running', 'summarizing', '正在总结这一段记录'],
        ['running', 'ready', '摘要已生成；在上下文占满前暂不启用'],
        ['success', 'applied', '已用摘要替换这一段记录'],
        ['failed', 'failed', '摘要生成失败；保留完整记录']
      ];
      for (const [status, phase, label] of cases) {
        const tool = trace(status, { start_step: 1, end_step: 4, phase });
        expect(getCompressionPhase(tool)).toBe(phase as any);
        expect(getCompressionPhaseLabel(tool)).toBe(label);
      }
    });

    it('falls back to status and note on traces that carry no phase', () => {
      expect(getCompressionPhase(trace('running', { start_step: 1, end_step: 4 }))).toBe('summarizing');
      expect(getCompressionPhase(trace('running', { start_step: 1, end_step: 4, note: 'retrying' }))).toBe('summarizing');
      expect(getCompressionPhase(trace('running', { start_step: 1, end_step: 4, note: 'held' }))).toBe('ready');
      expect(getCompressionPhase(trace('success', { start_step: 1, end_step: 4 }))).toBe('applied');
      expect(getCompressionPhase(trace('failed', { start_step: 1, end_step: 4 }))).toBe('failed');
      expect(getCompressionPhase(trace('running', { start_step: 1, end_step: 4, phase: 'bogus' }))).toBe('summarizing');
    });

    it('drives the timeline line from the phase even without a note', () => {
      expect(getCompressionLabel(trace('running', { start_step: 1, end_step: 4, phase: 'ready' })))
        .toBe('步骤 1–4 的简要记忆已就绪；在工作记忆占满前保留完整记录');
      expect(getCompressionLabel(trace('running', { start_step: 1, end_step: 4, phase: 'summarizing' })))
        .toBe('正在把 步骤 1–4 压缩为简要记忆以释放空间…');
    });

    it('treats a ready-but-held summary as waiting, not running', () => {
      expect(isCompressionWaiting(trace('running', { start_step: 1, end_step: 4, phase: 'ready' }))).toBeTrue();
      expect(isCompressionWaiting(trace('running', { start_step: 1, end_step: 4, note: 'held' }))).toBeTrue();
      expect(isCompressionWaiting(trace('running', { start_step: 1, end_step: 4, phase: 'summarizing' }))).toBeFalse();
      expect(isCompressionWaiting(trace('success', { start_step: 1, end_step: 4, phase: 'applied' }))).toBeFalse();
      expect(isCompressionWaiting({ type: 'tool', name: 'save_note', status: 'running', payload: { args: { phase: 'ready' } } })).toBeFalse();
    });
  });
});

describe('cleanErrorMessage', () => {
  it('removes repeated LLM wrapper labels while preserving the provider reason', () => {
    expect(cleanErrorMessage('LLM Error: LLM Request Error: 503 model overloaded'))
      .toBe('503 model overloaded');
  });

  it('does not present an empty LLM wrapper label as an error reason', () => {
    expect(cleanErrorMessage('LLM Error:')).toBe('未知错误');
  });
});

describe('getUniqueGenericTools retry aggregation', () => {
  it('groups recoverable LLM retries while preserving every attempt', () => {
    const tools = [
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 100,
        payload: { error: '503 first', delay: 1.18, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      },
      {
        trace_id: 'retry-2',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 101,
        payload: { error: '503 second', delay: 2.96, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      }
    ];

    const result = getUniqueGenericTools(tools);

    expect(result.length).toBe(1);
    expect(result[0].name).toBe('llm_retry_group');
    expect(result[0].payload.retry_count).toBe(2);
    expect(result[0].payload.total_delay).toBeCloseTo(4.14);
    expect(result[0].payload.retries.map((retry: any) => retry.error)).toEqual([
      '503 first',
      '503 second'
    ]);
  });

  it('folds SDK retry rows into their matching terminal failure', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        payload: { error: '503', delay: 1, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      },
      {
        trace_id: 'failure-1',
        type: 'llm_call',
        name: 'llm_pause',
        status: 'failed',
        payload: { error: '503 exhausted', pause: true, request_id: 'request-1', retries: [{ delay: 1 }] }
      }
    ]);

    expect(result.map(tool => tool.name)).toEqual(['llm_pause']);
  });

  it('does not expose opaque retries from non-Gemini providers', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'retry-openai',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        payload: { error: '429', delay: 2, provider: 'openai', source: 'artemis_wrapper' }
      },
      {
        trace_id: 'failure-openai',
        type: 'llm_call',
        name: 'llm_pause',
        status: 'failed',
        payload: { error: '429 exhausted', pause: true, waited_seconds: 12 }
      }
    ]);

    expect(result.map(tool => tool.name)).toEqual(['llm_pause']);
  });
});

describe('video analysis timeline formatting', () => {
  it('distinguishes a cache hit without presenting it as running', () => {
    const view = getVideoAnalysisView({
      name: 'spawn_sub_agent',
      status: 'success',
      payload: {
        args: { start_time: 0, end_time: 42 },
        result: 'CACHED VIDEO ANALYSIS: existing evidence'
      }
    });

    expect(view?.outcome).toBe('complete');
    expect(view?.reuse).toBe('full');
    expect(view?.title).toBe('复用已有的视频分析');
    expect(view?.requestedRange).toEqual({ start: 0, end: 42 });
  });

  it('collapses child video chunks into one stable note-style timeline item', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'chunk-1',
        parent_trace_id: 'video-agent-1',
        type: 'tool',
        name: 'spawn_sub_agent',
        status: 'success',
        timestamp: 100,
        payload: {
          args: { start_time: 0, end_time: 30, specific_query: 'find the result' },
          result: '[from 0.0s to 30.0s] Summary: first'
        }
      },
      {
        trace_id: 'chunk-2',
        parent_trace_id: 'video-agent-1',
        type: 'tool',
        name: 'spawn_sub_agent',
        status: 'success',
        timestamp: 101,
        payload: {
          args: { start_time: 30, end_time: 60, specific_query: 'find the result' },
          result: 'PARTIAL VIDEO ANALYSIS (successful chunks were persisted). Failed intervals: 45.0s-60.0s'
        }
      }
    ]);

    expect(result.length).toBe(1);
    expect(result[0].name).toBe('video_analysis');
    expect(result[0].trace_id).toBe('video-analysis-video-agent-1');
    expect(result[0].payload.result.outcome).toBe('partial');
    expect(result[0].payload.result.requested_range).toEqual({ start: 0, end: 60 });
    expect(getVideoAnalysisView(result[0])?.title).toBe('视频分析部分完成');
  });

  it('keeps video analyses from independent parent executions separate', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'chunk-1', parent_trace_id: 'video-agent-1', type: 'tool',
        name: 'spawn_sub_agent', status: 'success',
        payload: { args: { start_time: 0, end_time: 10 }, result: 'first' }
      },
      {
        trace_id: 'chunk-2', parent_trace_id: 'video-agent-2', type: 'tool',
        name: 'spawn_sub_agent', status: 'success',
        payload: { args: { start_time: 10, end_time: 20 }, result: 'second' }
      }
    ]);

    expect(result.map(tool => tool.trace_id)).toEqual([
      'video-analysis-video-agent-1',
      'video-analysis-video-agent-2'
    ]);
  });
});

describe('shouldShowTool (Option A Single Source of Truth)', () => {
  it('keeps ADB commands visible even when stepData.action_taken is present', () => {
    const adbTool = {
      name: 'run_adb_command',
      type: 'tool',
      payload: { CommandLine: 'dumpsys telephony.registry' }
    };
    const stepData = {
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000 }]
    };

    expect(shouldShowTool(adbTool, stepData)).toBeTrue();
  });

  it('keeps explorer calls visible when stepData.action_taken is present', () => {
    const explorerTool = {
      name: 'ask_explorer',
      type: 'tool'
    };
    const stepData = {
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000 }]
    };

    expect(shouldShowTool(explorerTool, stepData)).toBeTrue();
  });

  it('filters out internal plumbing tools', () => {
    const plumbingTool = {
      name: 'safety_net_pixel_validation',
      type: 'tool'
    };
    expect(shouldShowTool(plumbingTool)).toBeFalse();
  });
});

describe('getToolTargetText self-described targets', () => {
  it('uses target_description for coordinate tools when no target_text is observed', () => {
    expect(getToolTargetText({
      name: 'click',
      payload: { args: { coordinates: [500, 900], target_description: 'play button' } }
    })).toBe('play button');
    expect(getToolTargetText({
      name: 'exec_long_press',
      args: { coordinates: [500, 900], target_description: 'song row' }
    })).toBe('song row');
  });

  it('prefers observed target_text over target_description', () => {
    expect(getToolTargetText({
      name: 'click',
      payload: { args: { target_text: 'Play', target_description: 'play button' } }
    })).toBe('Play');
  });

  it('chains click_sequence target_descriptions with the coordinate arrow', () => {
    expect(getToolTargetText({
      name: 'click_sequence',
      payload: { args: { sequence: [[500, 300], [876, 360]], target_descriptions: ['play button', 'close ×'] } }
    })).toBe('play button → close ×');
  });

  it('falls back gracefully when click_sequence descriptions are missing or short', () => {
    expect(getToolTargetText({
      name: 'click_sequence',
      payload: { args: { sequence: [[500, 300], [876, 360]] } }
    })).toBe('');
    expect(getToolTargetText({
      name: 'click_sequence',
      payload: { args: { sequence: [[500, 300], [876, 360]], target_descriptions: ['play button'] } }
    })).toBe('play button');
    expect(getToolTargetText({
      name: 'click_sequence',
      payload: { args: { sequence: [[500, 300]], target_descriptions: [null, ''], target_description: 'fallback' } }
    })).toBe('fallback');
    expect(joinTargetDescriptions(undefined)).toBe('');
    expect(joinTargetDescriptions('play button')).toBe('');
  });
});

describe('extractToolExtraParams', () => {
  it('hides target_description and target_descriptions from the extra parameter list', () => {
    const keys = extractToolExtraParams({
      name: 'click_sequence',
      args: { sequence: [[1, 2]], target_descriptions: ['a'], target_description: 'b', interval_ms: 50 }
    }).map(p => p.key);
    expect(keys).not.toContain('Target Descriptions');
    expect(keys).not.toContain('Target Description');
    expect(keys).toContain('Interval Ms');
  });
});
