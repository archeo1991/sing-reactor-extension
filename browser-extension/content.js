(() => {
  'use strict';

  if (window.__blsAssistantLoaded) return;
  window.__blsAssistantLoaded = true;

  const P = 'bls-';
  const shared = window.BLSShared;
  const storage = window.BLSStorage;

  const state = {
    video: null,
    lyrics: [],
    source: '',
    correction: null,
    activeIndex: 0,
    collapsed: true,
    view: 'empty',
    sentenceMode: false,
    loop: false,
    repeatTarget: 1,
    repeatDone: 0,
    speed: 1,
    recognizing: false,
    pageKey: '',
    lastUrl: `${location.origin}${location.pathname}${location.search}`,
    requestEpoch: 0,
    endHandled: false,
    saveTimer: null,
    lastSavedJson: '',
    playbackCursor: -1,
    highlightedIndex: -1,
    monitorTimer: null,
    videoScanTimer: null,
    activeJobId: ''
  };

  const el = {};

  function formatTime(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    const minutes = Math.floor(safe / 60);
    const secs = Math.floor(safe % 60);
    const tenths = Math.floor((safe % 1) * 10);
    return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${tenths}`;
  }

  function formatRangeTime(seconds) {
    const safe = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${String(Math.floor(safe / 60)).padStart(2, '0')}:${String(safe % 60).padStart(2, '0')}`;
  }

  function getPageKey() {
    const bvid = location.pathname.match(/\/video\/(BV[\w]+)/i)?.[1];
    const page = new URL(location.href).searchParams.get('p') || '1';
    if (bvid) return `bvid:${bvid.toUpperCase()}:p${page}`;
    return `url:${location.origin}${location.pathname}`;
  }

  function currentRequest(epoch, pageKey) {
    return epoch === state.requestEpoch && pageKey === state.pageKey && pageKey === getPageKey();
  }

  function discardStaleRequest(epoch) {
    if (epoch !== state.requestEpoch) return;
    state.recognizing = false;
    if (state.view === 'recognizing') setView('empty');
    watchPage();
  }

  function isEditable(target) {
    return target instanceof HTMLElement && (
      target.isContentEditable ||
      /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) ||
      Boolean(target.closest('[contenteditable="true"]'))
    );
  }

  function buildPanel() {
    const root = document.createElement('section');
    root.id = `${P}root`;
    root.innerHTML = `
      <button class="${P}edge-tab" data-action="collapse" type="button" aria-label="展开声迹"><img src="${chrome.runtime.getURL('logo.png')}" alt=""></button>
      <div class="${P}panel">
        <header class="${P}header">
          <div class="${P}brand">
            <img class="${P}brand-mark" src="${chrome.runtime.getURL('logo.png')}" alt="声迹 Logo">
            <div><strong>声迹</strong><small>听见每一句，唱准每一拍</small></div>
          </div>
          <button class="${P}icon-btn" data-action="collapse" type="button" title="收起声迹" aria-label="收起声迹">›</button>
        </header>
        <div class="${P}video-status"><i></i><span>正在寻找播放器…</span></div>
        <main class="${P}body">
          <section class="${P}view ${P}empty-view">
            <div class="${P}empty-art"><span>♪</span></div>
            <h2>从一句开始，唱得更好</h2>
            <p>默认使用声迹服务搜索并同步公开 LRC；B站字幕作为可选快速来源。</p>
            <div class="${P}range-card">
              <strong>歌曲范围</strong>
              <div class="${P}range-grid">
                <label>开始<input class="${P}range-start" value="00:00" inputmode="numeric" placeholder="mm:ss"></label>
                <button data-action="use-start" type="button">使用当前播放位置</button>
                <label>结束<input class="${P}range-end" value="00:00" inputmode="numeric" placeholder="mm:ss"></label>
                <button data-action="use-end" type="button">使用当前播放位置</button>
              </div>
              <p class="${P}range-error" hidden></p>
            </div>
            <button class="${P}primary" data-action="recognize" type="button">获取歌词</button>
            <button class="${P}secondary ${P}subtitle-main" data-action="subtitle" type="button">使用 B站字幕</button>
            <div class="${P}fallback" hidden>
              <div class="${P}error-card"><strong class="${P}error-title">未能获取歌词</strong><p class="${P}error-message"></p></div>
              <p class="${P}fallback-hint">可以尝试 B站字幕，或返回调整歌曲范围。</p>
              <button class="${P}primary" data-action="subtitle" type="button">使用 B站字幕</button>
              <div class="${P}inline-actions">
                <button data-action="local" type="button">使用声迹服务</button>
                <button data-action="edit-range" type="button">返回修改范围</button>
              </div>
            </div>
            <div class="${P}divider"><span>或粘贴 LRC</span></div>
            <textarea class="${P}lrc-input" placeholder="[00:12.50] 在这里粘贴歌词…" spellcheck="false"></textarea>
            <button class="${P}text-btn" data-action="import" type="button">导入 LRC 文本</button>
          </section>

          <section class="${P}view ${P}recognize-view" hidden>
            <div class="${P}scan"><span>♫</span><i></i></div>
            <h2>正在获取歌词</h2>
            <p class="${P}recognize-stage">正在连接声迹服务…</p>
            <div class="${P}progress"><i></i></div>
            <p class="${P}hint">服务会下载并识别所选片段，耗时取决于片段长度和当前任务量。</p>
          </section>

          <section class="${P}view ${P}practice-view" hidden>
            <div class="${P}source-row"><span>识别来源</span><b class="${P}source-label">手动导入</b></div>
            <div class="${P}correction-status" hidden><span></span><a target="_blank" rel="noopener noreferrer">查看来源</a></div>
            <div class="${P}now-card">
              <div class="${P}now-meta"><span>当前句</span><time>00:00.0 — 00:00.0</time></div>
              <textarea class="${P}lyric-editor" rows="2" aria-label="修改当前歌词"></textarea>
              <div class="${P}adjust-row">
                <div><span>起点</span><button data-adjust="start:-0.1">−</button><b class="${P}start-value">0.0s</b><button data-adjust="start:0.1">＋</button></div>
                <div><span>终点</span><button data-adjust="end:-0.1">−</button><b class="${P}end-value">0.0s</b><button data-adjust="end:0.1">＋</button></div>
              </div>
            </div>

            <div class="${P}transport">
              <button data-action="prev" type="button" title="上一句（←）">‹</button>
              <button data-action="replay" type="button" title="重播（R）">↺</button>
              <button class="${P}play" data-action="play" type="button" title="播放/暂停（空格）">▶</button>
              <button data-action="next" type="button" title="下一句（→）">›</button>
              <button data-action="master" type="button" title="标记已掌握（Enter）">✓</button>
            </div>

            <div class="${P}options">
              <label><input class="${P}sentence-mode" type="checkbox"><span>逐句暂停</span></label>
              <label><input class="${P}loop-mode" type="checkbox"><span>单句循环</span></label>
              <label>重复<select class="${P}repeat-select" disabled><option>1</option><option>2</option><option>3</option><option>5</option><option value="Infinity">∞</option></select></label>
              <label>速度<select class="${P}speed-select"><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option></select></label>
            </div>

            <div class="${P}list-head"><strong>歌词时间轴</strong><span class="${P}count"></span></div>
            <div class="${P}lyric-list" role="list"></div>
            <button class="${P}replace-btn" data-action="back" type="button">重新获取或导入歌词</button>
          </section>
        </main>
      </div>`;
    root.classList.toggle(`${P}collapsed`, state.collapsed);
    document.documentElement.appendChild(root);

    el.root = root;
    el.panel = root.querySelector(`.${P}panel`);
    el.edgeTab = root.querySelector(`.${P}edge-tab`);
    el.videoStatus = root.querySelector(`.${P}video-status`);
    el.emptyView = root.querySelector(`.${P}empty-view`);
    el.recognizeView = root.querySelector(`.${P}recognize-view`);
    el.practiceView = root.querySelector(`.${P}practice-view`);
    el.lrcInput = root.querySelector(`.${P}lrc-input`);
    el.recognizeStage = root.querySelector(`.${P}recognize-stage`);
    el.rangeStart = root.querySelector(`.${P}range-start`);
    el.rangeEnd = root.querySelector(`.${P}range-end`);
    el.rangeError = root.querySelector(`.${P}range-error`);
    el.fallback = root.querySelector(`.${P}fallback`);
    el.fallbackHint = root.querySelector(`.${P}fallback-hint`);
    el.errorTitle = root.querySelector(`.${P}error-title`);
    el.errorMessage = root.querySelector(`.${P}error-message`);
    el.subtitleMain = root.querySelector(`.${P}subtitle-main`);
    el.subtitleButtons = [...root.querySelectorAll('[data-action="subtitle"]')];
    el.sourceLabel = root.querySelector(`.${P}source-label`);
    el.correctionStatus = root.querySelector(`.${P}correction-status`);
    el.correctionText = el.correctionStatus.querySelector('span');
    el.correctionLink = el.correctionStatus.querySelector('a');
    el.nowTime = root.querySelector(`.${P}now-meta time`);
    el.editor = root.querySelector(`.${P}lyric-editor`);
    el.startValue = root.querySelector(`.${P}start-value`);
    el.endValue = root.querySelector(`.${P}end-value`);
    el.list = root.querySelector(`.${P}lyric-list`);
    el.count = root.querySelector(`.${P}count`);
    el.play = root.querySelector(`.${P}play`);
    el.sentenceMode = root.querySelector(`.${P}sentence-mode`);
    el.loopMode = root.querySelector(`.${P}loop-mode`);
    el.repeatSelect = root.querySelector(`.${P}repeat-select`);
    el.speedSelect = root.querySelector(`.${P}speed-select`);

    root.addEventListener('click', onPanelClick);
    el.editor.addEventListener('change', updateLyricText);
    el.sentenceMode.addEventListener('change', () => {
      state.sentenceMode = el.sentenceMode.checked;
      el.repeatSelect.disabled = !state.sentenceMode;
      state.repeatDone = 0;
      state.endHandled = false;
      const found = state.video && state.lyrics.findIndex((line, index) =>
        state.video.currentTime >= line.start && (state.video.currentTime < line.end || index === state.lyrics.length - 1)
      );
      if (state.sentenceMode && found >= 0 && found !== state.activeIndex) {
        state.activeIndex = found;
        state.playbackCursor = found;
        updateCurrentCard(true);
      }
      updatePlaybackMonitoring();
    });
    el.loopMode.addEventListener('change', () => { state.loop = el.loopMode.checked; state.repeatDone = 0; state.endHandled = false; updatePlaybackMonitoring(); });
    el.repeatSelect.addEventListener('change', () => { state.repeatTarget = Number(el.repeatSelect.value); state.repeatDone = 0; state.endHandled = false; });
    el.speedSelect.addEventListener('change', () => setSpeed(Number(el.speedSelect.value)));
  }

  function setView(view) {
    state.view = view;
    el.emptyView.hidden = view !== 'empty';
    el.recognizeView.hidden = view !== 'recognizing';
    el.practiceView.hidden = view !== 'practice';
    updatePlaybackMonitoring();
  }

  function onPanelClick(event) {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.adjust) {
      const [field, amount] = button.dataset.adjust.split(':');
      adjustTime(field, Number(amount));
      return;
    }
    const action = button.dataset.action;
    const actions = {
      collapse: toggleCollapse,
      recognize: startLocalRecognition,
      subtitle: loadBilibiliSubtitles,
      local: startLocalRecognition,
      import: importLrc,
      'use-start': () => useCurrentPosition(el.rangeStart),
      'use-end': () => useCurrentPosition(el.rangeEnd),
      'edit-range': hideFallback,
      prev: () => selectSentence(state.activeIndex - 1, true),
      next: () => selectSentence(state.activeIndex + 1, true),
      replay: replayCurrent,
      play: togglePlay,
      master: markMastered,
      back: () => { ++state.requestEpoch; cancelActiveJob(); state.recognizing = false; hideFallback(); setView('empty'); fillDefaultRange(); }
    };
    if (actions[action]) actions[action]();
    const row = button.closest(`.${P}lyric-row`);
    if (row && !action) selectSentence(Number(row.dataset.index), true);
  }

  function toggleCollapse() {
    state.collapsed = !state.collapsed;
    el.root.classList.toggle(`${P}collapsed`, state.collapsed);
  }

  function parseTime(raw, allowFraction = true) {
    const fractionPattern = allowFraction ? '(?:[.:](\\d{1,3}))?' : '';
    const match = String(raw).trim().match(new RegExp(`^(\\d{1,3}):(\\d{2})${fractionPattern}$`));
    if (!match) return null;
    const seconds = Number(match[2]);
    if (seconds >= 60) return null;
    const fraction = match[3] ? Number(`0.${match[3]}`) : 0;
    return Number(match[1]) * 60 + seconds + fraction;
  }

  function parseLrc(text) {
    const rows = [];
    text.split(/\r?\n/).forEach(line => {
      const stamps = [...line.matchAll(/\[(\d{1,3}:\d{2}(?:[.:]\d{1,3})?)\]/g)];
      const lyric = line.replace(/\[[^\]]+\]/g, '').trim();
      if (!lyric) return;
      stamps.forEach(stamp => {
        const start = parseTime(stamp[1]);
        if (start !== null) rows.push({ start, end: start + 4, text: lyric, mastered: false });
      });
    });
    rows.sort((a, b) => a.start - b.start);
    rows.forEach((row, index) => {
      if (rows[index + 1]) row.end = Math.max(row.start + 0.1, rows[index + 1].start);
    });
    return rows;
  }

  function importLrc() {
    const parsed = parseLrc(el.lrcInput.value);
    if (!parsed.length) {
      el.lrcInput.classList.add(`${P}invalid`);
      el.lrcInput.placeholder = '未找到有效时间标签，例如：[00:12.50] 歌词';
      setTimeout(() => el.lrcInput.classList.remove(`${P}invalid`), 800);
      return;
    }
    useLyrics(parsed, 'LRC 手动导入');
  }

  function usableDuration() {
    return state.video && Number.isFinite(state.video.duration) ? state.video.duration : 0;
  }

  function fillDefaultRange() {
    if (!el.rangeStart.value) el.rangeStart.value = '00:00';
    const duration = usableDuration();
    if (duration > 0 && (el.rangeEnd.value === '00:00' || !parseTime(el.rangeEnd.value, false))) {
      el.rangeEnd.value = formatRangeTime(Math.min(Math.ceil(duration), shared.MAX_RANGE_SECONDS));
    }
  }

  function useCurrentPosition(input) {
    if (!state.video) {
      showRangeError('尚未连接播放器，无法读取当前播放位置。');
      return;
    }
    input.value = formatRangeTime(state.video.currentTime);
    clearRangeError();
  }

  function readRange() {
    const start = parseTime(el.rangeStart.value, false);
    const end = parseTime(el.rangeEnd.value, false);
    if (start === null || end === null) {
      showRangeError('请输入有效时间，格式为 mm:ss。');
      return null;
    }
    if (start >= end) {
      showRangeError('开始时间必须早于结束时间。');
      return null;
    }
    if (end - start > shared.MAX_RANGE_SECONDS) {
      showRangeError('单次获取范围不能超过 15 分钟。');
      return null;
    }
    clearRangeError();
    return { start, end };
  }

  function showRangeError(message) {
    el.rangeError.textContent = message;
    el.rangeError.hidden = false;
  }

  function clearRangeError() {
    el.rangeError.hidden = true;
    el.rangeError.textContent = '';
  }

  function hideFallback(focus = true) {
    el.fallback.hidden = true;
    updateSubtitleAvailability();
    clearRangeError();
    if (focus) el.rangeStart.focus();
  }

  function showFallback(message, title = '未能获取歌词') {
    state.recognizing = false;
    setView('empty');
    el.errorTitle.textContent = title;
    el.errorMessage.textContent = message;
    el.fallback.hidden = false;
    updateSubtitleAvailability();
  }

  async function sendMessage(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (error) {
      return { ok: false, error: error?.message || '扩展后台不可用，请重新加载扩展后重试。' };
    }
  }

  function isBangumiPage() {
    return location.pathname.startsWith('/bangumi/play/');
  }

  function updateSubtitleAvailability() {
    const unavailable = isBangumiPage();
    el.subtitleButtons.forEach(button => {
      button.hidden = unavailable || (button === el.subtitleMain && !el.fallback.hidden);
      button.disabled = unavailable;
    });
    el.fallbackHint.textContent = unavailable
      ? '可以返回调整歌曲范围，或使用声迹服务。'
      : '可以尝试 B站字幕，或返回调整歌曲范围。';
  }

  async function loadBilibiliSubtitles() {
    if (isBangumiPage()) return;
    const epoch = ++state.requestEpoch;
    const requestPageKey = state.pageKey;
    if (!await cancelActiveJob()) {
      showFallback('取消上一个识别任务失败，将保留任务并稍后重试。', '服务识别失败');
      return;
    }
    if (!currentRequest(epoch, requestPageKey)) return;
    state.recognizing = true;
    el.fallback.hidden = true;
    setView('recognizing');
    el.recognizeStage.textContent = '正在读取 B站视频信息与字幕…';
    const result = await sendMessage({ type: shared.MESSAGE_TYPES.FETCH_SUBTITLES, url: location.href });
    if (!currentRequest(epoch, requestPageKey)) {
      discardStaleRequest(epoch);
      return;
    }
    state.recognizing = false;
    if (result?.ok && result.found && result.lyrics?.length) {
      useLyrics(result.lyrics, result.source || 'B站字幕');
      return;
    }
    showFallback(result?.reason || result?.error || '未找到可用的 B站字幕，可使用声迹服务识别。');
  }

  const JOB_STAGE_TEXT = Object.freeze({
    queued: '任务已排队…', starting: '正在启动识别任务…', cache_lookup: '正在检查本地缓存…',
    downloading: '正在下载音频片段…', ffmpeg: '正在截取并转换音频…', whisper: '正在进行语音识别…',
    lyrics: '正在搜索并同步歌词…', cache_write: '正在保存识别结果…', cancelling: '正在取消识别任务…', completed: '识别完成'
  });

  function sleep(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  }

  async function readActiveJobs() {
    try {
      const stored = await chrome.storage.local.get(shared.ACTIVE_JOBS_STORAGE_KEY);
      const jobs = stored?.[shared.ACTIVE_JOBS_STORAGE_KEY];
      return jobs && typeof jobs === 'object' ? jobs : {};
    } catch (_) {
      return {};
    }
  }

  async function persistActiveJob(pageKey, jobId) {
    if (!pageKey) return;
    try {
      const jobs = await readActiveJobs();
      if (jobId) jobs[pageKey] = jobId;
      else delete jobs[pageKey];
      await chrome.storage.local.set({ [shared.ACTIVE_JOBS_STORAGE_KEY]: jobs });
    } catch (_) {}
  }

  async function cancelJob(jobId, pageKey, attempts = shared.JOB_POLL_MAX_RETRIES) {
    if (!jobId) return true;
    for (let failure = 1; failure <= attempts; failure += 1) {
      const result = await sendMessage({ type: shared.MESSAGE_TYPES.CANCEL_TRANSCRIBE_JOB, jobId });
      if (result?.ok) {
        await persistActiveJob(pageKey, '');
        if (state.activeJobId === jobId) state.activeJobId = '';
        return true;
      }
      if (failure < attempts) await sleep(shared.nextPollDelay(failure));
    }
    return false;
  }

  async function cancelActiveJob() {
    return cancelJob(state.activeJobId, state.pageKey);
  }

  async function pollRecognitionJob(jobId, epoch, requestPageKey) {
    let failures = 0;
    while (currentRequest(epoch, requestPageKey) && state.activeJobId === jobId) {
      const job = await sendMessage({ type: shared.MESSAGE_TYPES.GET_TRANSCRIBE_JOB, jobId });
      if (!currentRequest(epoch, requestPageKey)) {
        await cancelJob(jobId, requestPageKey);
        discardStaleRequest(epoch);
        return;
      }
      if (!job?.ok) {
        failures += 1;
        if (shared.shouldRetryJobOperation(job, failures)) {
          el.recognizeStage.textContent = `连接暂时失败，正在重试（${failures}/${shared.JOB_POLL_MAX_RETRIES}）…`;
          await sleep(shared.nextPollDelay(failures));
          continue;
        }
        showFallback(job?.error || '读取识别任务失败。', '服务识别失败');
        return;
      }
      failures = 0;
      el.recognizeStage.textContent = JOB_STAGE_TEXT[job.stage] || `正在处理：${job.stage || job.status}`;
      if (job.status === 'succeeded') {
        state.activeJobId = '';
        await persistActiveJob(requestPageKey, '');
        state.recognizing = false;
        applyTranscriptionResult(job.result);
        return;
      }
      if (job.status === 'failed' || job.status === 'cancelled') {
        state.activeJobId = '';
        await persistActiveJob(requestPageKey, '');
        showFallback(job.error || (job.status === 'cancelled' ? '识别任务已取消。' : '服务识别任务失败。'), '服务识别失败');
        return;
      }
      await sleep(shared.JOB_POLL_INTERVAL_MS);
    }
  }

  async function startLocalRecognition() {
    const range = readRange();
    if (!range) return;
    const epoch = ++state.requestEpoch;
    const requestPageKey = state.pageKey;
    if (!await cancelActiveJob()) {
      showFallback('取消上一个识别任务失败，将保留任务并稍后重试。', '服务识别失败');
      return;
    }
    if (!currentRequest(epoch, requestPageKey)) return;
    state.recognizing = true;
    setView('recognizing');
    el.recognizeStage.textContent = '正在创建识别任务…';
    const cleanUrl = new URL(location.href);
    cleanUrl.hash = '';
    const created = await sendMessage({
      type: shared.MESSAGE_TYPES.CREATE_TRANSCRIBE_JOB,
      payload: { url: cleanUrl.href, start: range.start, end: range.end, language: 'auto' }
    });
    if (!currentRequest(epoch, requestPageKey)) {
      if (created?.jobId) {
        await persistActiveJob(requestPageKey, created.jobId);
        await cancelJob(created.jobId, requestPageKey);
      }
      discardStaleRequest(epoch);
      return;
    }
    if (!created?.ok || !created.jobId) {
      showFallback(created?.error || '无法创建识别任务。', '服务识别失败');
      return;
    }
    state.activeJobId = created.jobId;
    await persistActiveJob(requestPageKey, created.jobId);
    await pollRecognitionJob(created.jobId, epoch, requestPageKey);
  }

  function applyTranscriptionResult(result) {
    if (result?.segments?.length) {
      const lyrics = result.segments.map(segment => ({
        start: Number(segment.start), end: Number(segment.end), text: String(segment.text || '').trim(), mastered: false
      })).filter(line => Number.isFinite(line.start) && Number.isFinite(line.end) && line.end > line.start && line.text);
      if (lyrics.length) {
        const correction = result.correction && typeof result.correction === 'object' ? result.correction : null;
        const source = correction?.mode === 'lrc_timeline_synced'
          ? '网页 LRC + 服务音频校准'
          : (correction?.mode === 'lyrics_timeline_rebuilt'
            ? 'Faster-Whisper + 歌词时间轴对齐'
            : (correction?.mode === 'text_only' ? 'Faster-Whisper + 联网歌词校正' : 'Faster-Whisper（未校正）'));
        useLyrics(lyrics, source, correction);
        return;
      }
    }
    showFallback('声迹服务没有返回有效分段，请调整范围后重试。', '服务识别失败');
  }

  function normalizeLyrics(lyrics) {
    const normalized = lyrics.map(line => ({
      start: Number(line.start),
      end: Number(line.end),
      text: String(line.text || '').trim(),
      mastered: line.mastered === true
    })).filter(line => Number.isFinite(line.start) && Number.isFinite(line.end) && line.start >= 0 && line.end > line.start && line.text)
      .sort((a, b) => a.start - b.start);
    normalized.forEach((line, index) => {
      const next = normalized[index + 1];
      if (next && line.end > next.start) line.end = next.start;
    });
    return normalized.filter(line => line.end - line.start >= shared.MIN_LINE_DURATION);
  }

  function useLyrics(lyrics, source = '未知来源', correction = null, shouldSave = true) {
    state.lyrics = normalizeLyrics(lyrics);
    state.source = source;
    state.correction = correction;
    state.activeIndex = 0;
    state.playbackCursor = 0;
    state.repeatDone = 0;
    state.endHandled = false;
    setView('practice');
    renderList();
    updateCurrentCard();
    el.sourceLabel.textContent = source;
    renderCorrectionStatus(correction);
    if (shouldSave) saveData();
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || ''));
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function renderCorrectionStatus(correction) {
    if (!correction || typeof correction !== 'object') {
      el.correctionStatus.hidden = true;
      el.correctionText.textContent = '';
      el.correctionLink.removeAttribute('href');
      return;
    }
    const matched = Math.max(0, Number(correction.matched_count) || 0);
    const total = Math.max(0, Number(correction.total_count) || state.lyrics.length);
    const timedLineCount = Math.max(0, Number(correction.timed_line_count) || 0);
    const reason = String(correction.reason || '').trim();
    el.correctionText.textContent = correction.mode === 'lrc_timeline_synced'
      ? `已同步网页 LRC，共 ${timedLineCount} 行`
      : (correction.mode === 'lyrics_timeline_rebuilt'
        ? `已根据网页歌词重建 ${timedLineCount} 行时间轴`
        : (correction.mode === 'text_only'
          ? `已根据网页歌词校正 ${matched}/${total} 句`
          : (reason || '未找到质量足够的网页歌词，已保留模型结果')));
    const sourceUrl = safeHttpUrl(correction.source_url);
    if (sourceUrl) {
      el.correctionLink.href = sourceUrl;
      el.correctionLink.hidden = false;
    } else {
      el.correctionLink.removeAttribute('href');
      el.correctionLink.hidden = true;
    }
    el.correctionStatus.hidden = false;
  }

  function renderList() {
    el.list.textContent = '';
    const fragment = document.createDocumentFragment();
    state.lyrics.forEach((line, index) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `${P}lyric-row`;
      row.dataset.index = index;
      row.setAttribute('role', 'listitem');
      if (index === state.activeIndex) row.classList.add(`${P}active`);
      if (line.mastered) row.classList.add(`${P}mastered`);
      const timeEl = document.createElement('time');
      timeEl.textContent = formatTime(line.start);
      const textEl = document.createElement('span');
      textEl.textContent = line.text;
      const badgeEl = document.createElement('i');
      badgeEl.textContent = line.mastered ? '已掌握' : '待练习';
      row.replaceChildren(timeEl, textEl, badgeEl);
      fragment.appendChild(row);
    });
    el.list.appendChild(fragment);
    state.highlightedIndex = state.activeIndex;
    const mastered = state.lyrics.filter(line => line.mastered).length;
    el.count.textContent = `${mastered}/${state.lyrics.length} 已掌握`;
  }

  function updateCurrentCard(scroll = false) {
    const line = state.lyrics[state.activeIndex];
    if (!line) return;
    el.nowTime.textContent = `${formatTime(line.start)} — ${formatTime(line.end)}`;
    if (document.activeElement !== el.editor) el.editor.value = line.text;
    el.startValue.textContent = `${line.start.toFixed(1)}s`;
    el.endValue.textContent = `${line.end.toFixed(1)}s`;
    if (state.highlightedIndex !== state.activeIndex) {
      el.list.children[state.highlightedIndex]?.classList.remove(`${P}active`);
      el.list.children[state.activeIndex]?.classList.add(`${P}active`);
      state.highlightedIndex = state.activeIndex;
    }
    if (scroll) el.list.children[state.activeIndex]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  function selectSentence(index, seek = false) {
    if (!state.lyrics.length) return;
    state.activeIndex = Math.max(0, Math.min(state.lyrics.length - 1, index));
    state.playbackCursor = state.activeIndex;
    state.repeatDone = 0;
    state.endHandled = false;
    updateCurrentCard(true);
    if (seek && state.video) state.video.currentTime = state.lyrics[state.activeIndex].start;
  }

  function replayCurrent() {
    const line = state.lyrics[state.activeIndex];
    if (!state.video || !line) return;
    state.endHandled = false;
    state.video.currentTime = line.start;
    state.video.play().catch(() => {});
  }

  function togglePlay() {
    if (!state.video) return;
    if (state.video.paused) {
      const line = state.lyrics[state.activeIndex];
      if (state.sentenceMode && state.endHandled && line && state.video.currentTime >= line.end - 0.15) {
        state.video.currentTime = line.start;
        state.repeatDone = 0;
        state.endHandled = false;
      }
      state.video.play().catch(() => {});
    } else {
      state.video.pause();
    }
  }

  function markMastered() {
    const line = state.lyrics[state.activeIndex];
    if (!line) return;
    const newlyMastered = !line.mastered;
    line.mastered = newlyMastered;
    renderList();
    updateCurrentCard();
    saveData();
    if (newlyMastered && state.activeIndex < state.lyrics.length - 1) selectSentence(state.activeIndex + 1, true);
  }

  function updateLyricText() {
    const line = state.lyrics[state.activeIndex];
    if (!line) return;
    line.text = el.editor.value.trim() || '（空白歌词）';
    renderList();
    updateCurrentCard();
    saveData();
  }

  function adjustTime(field, amount) {
    const index = state.activeIndex;
    const line = state.lyrics[index];
    if (!line) return;
    const previous = state.lyrics[index - 1];
    const next = state.lyrics[index + 1];
    const minimum = shared.MIN_LINE_DURATION;
    if (field === 'start') {
      const lower = previous ? previous.start + minimum : 0;
      line.start = Math.max(lower, Math.min(line.end - minimum, line.start + amount));
      if (previous) previous.end = Math.min(line.start, Math.max(previous.start + minimum, previous.end));
    } else {
      const upper = next ? next.end - minimum : Infinity;
      line.end = Math.min(upper, Math.max(line.start + minimum, line.end + amount));
      if (next) next.start = Math.max(line.end, Math.min(next.end - minimum, next.start));
    }
    [previous, line, next].filter(Boolean).forEach(item => {
      item.start = Math.round(item.start * 10) / 10;
      item.end = Math.round(item.end * 10) / 10;
    });
    state.playbackCursor = index;
    renderList();
    updateCurrentCard();
    saveData();
  }

  function setSpeed(speed) {
    state.speed = speed;
    if (state.video) state.video.playbackRate = speed;
  }

  function findBestVideo() {
    let best = null;
    let bestScore = -1;
    document.querySelectorAll('video').forEach(video => {
      const score = (video.clientWidth * video.clientHeight) + (video.readyState > 0 ? 100000 : 0);
      if (score > bestScore) {
        best = video;
        bestScore = score;
      }
    });
    return best;
  }

  function attachVideo(video) {
    if (!video || video === state.video) return;
    if (state.video) {
      state.video.removeEventListener('play', handleVideoPlay);
      state.video.removeEventListener('pause', syncPlayButton);
      state.video.removeEventListener('loadedmetadata', fillDefaultRange);
      state.video.removeEventListener('timeupdate', monitorPlayback);
    }
    state.video = video;
    state.video.playbackRate = state.speed;
    state.video.addEventListener('play', handleVideoPlay);
    state.video.addEventListener('pause', syncPlayButton);
    state.video.addEventListener('loadedmetadata', fillDefaultRange);
    state.video.addEventListener('timeupdate', monitorPlayback);
    el.videoStatus.classList.add(`${P}connected`);
    el.videoStatus.querySelector('span').textContent = '已连接当前播放器';
    syncPlayButton();
    fillDefaultRange();
  }

  function syncPlayButton() {
    if (!state.video) return;
    el.play.textContent = state.video.paused ? '▶' : '❚❚';
    el.play.classList.toggle(`${P}is-playing`, !state.video.paused);
    updatePlaybackMonitoring();
  }

  function handleVideoPlay() {
    syncPlayButton();
    if (!state.sentenceMode || !state.endHandled) return;
    const line = state.lyrics[state.activeIndex];
    if (!line || state.video.currentTime < line.end - 0.15) return;
    state.video.currentTime = line.start;
    state.repeatDone = 0;
    state.endHandled = false;
  }

  function findLyricIndex(time) {
    const cursor = state.playbackCursor;
    const current = state.lyrics[cursor];
    if (current && time >= current.start && (time < current.end || cursor === state.lyrics.length - 1)) return cursor;
    const next = state.lyrics[cursor + 1];
    if (next && time >= next.start && (time < next.end || cursor + 1 === state.lyrics.length - 1)) return cursor + 1;
    let low = 0;
    let high = state.lyrics.length - 1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      const line = state.lyrics[middle];
      if (time < line.start) high = middle - 1;
      else if (time >= line.end && middle < state.lyrics.length - 1) low = middle + 1;
      else return middle;
    }
    return -1;
  }

  function updatePlaybackMonitoring() {
    clearInterval(state.monitorTimer);
    state.monitorTimer = null;
    if (document.hidden || state.view !== 'practice' || !state.video || state.video.paused || (!state.sentenceMode && !state.loop)) return;
    state.monitorTimer = setInterval(monitorPlayback, 80);
  }

  function monitorPlayback() {
    const video = state.video;
    if (document.hidden || !video || !state.lyrics.length || state.view !== 'practice') return;
    const time = video.currentTime;
    let current = state.lyrics[state.activeIndex];

    if (!video.paused && (state.sentenceMode || state.loop) && current && time >= current.end - 0.035 && !state.endHandled) {
      state.endHandled = true;
      const infinite = state.loop || state.repeatTarget === Infinity;
      if (infinite || state.repeatDone + 1 < state.repeatTarget) {
        state.repeatDone += 1;
        video.currentTime = current.start;
        video.play().catch(() => {});
        setTimeout(() => { state.endHandled = false; }, 120);
      } else {
        video.pause();
        video.currentTime = current.end;
        state.repeatDone = 0;
      }
      return;
    }

    if (!state.loop) {
      const found = findLyricIndex(time);
      const canFollow = !state.sentenceMode || !state.endHandled || !video.paused;
      if (canFollow && found >= 0 && found !== state.activeIndex) {
        state.playbackCursor = found;
        state.activeIndex = found;
        state.repeatDone = 0;
        state.endHandled = false;
        updateCurrentCard(true);
        current = state.lyrics[state.activeIndex];
      }
    }
    if (current && time < current.end - 0.15) state.endHandled = false;
  }

  function saveData() {
    clearTimeout(state.saveTimer);
    if (!state.lyrics.length || !state.pageKey) return;
    const snapshot = {
      lyrics: state.lyrics.map(line => ({ ...line })),
      source: state.source,
      correction: state.correction ? { ...state.correction } : null
    };
    const json = JSON.stringify(snapshot);
    if (json === state.lastSavedJson) return;
    const savePageKey = state.pageKey;
    state.saveTimer = setTimeout(async () => {
      if (savePageKey !== state.pageKey) return;
      if (await storage.save(savePageKey, snapshot)) state.lastSavedJson = json;
    }, 180);
  }

  async function loadForPage() {
    const epoch = ++state.requestEpoch;
    const previousPageKey = state.pageKey;
    const previousJobId = state.activeJobId;
    if (previousJobId) await cancelJob(previousJobId, previousPageKey);
    const nextPageKey = getPageKey();
    state.pageKey = nextPageKey;
    state.lyrics = [];
    state.source = '';
    state.correction = null;
    state.activeIndex = 0;
    state.playbackCursor = -1;
    state.highlightedIndex = -1;
    state.recognizing = false;
    state.lastSavedJson = '';
    clearTimeout(state.saveTimer);
    el.rangeStart.value = '00:00';
    el.rangeEnd.value = '00:00';
    hideFallback(false);
    setView('empty');
    fillDefaultRange();
    const activeJobs = await readActiveJobs();
    const restoredJobId = activeJobs[nextPageKey];
    if (restoredJobId && currentRequest(epoch, nextPageKey)) {
      state.activeJobId = restoredJobId;
      state.recognizing = true;
      setView('recognizing');
      el.recognizeStage.textContent = '正在恢复识别任务…';
      await pollRecognitionJob(restoredJobId, epoch, nextPageKey);
      return;
    }
    const saved = await storage.load(nextPageKey);
    if (!currentRequest(epoch, nextPageKey)) return;
    if (saved?.lyrics?.length) {
      state.lastSavedJson = JSON.stringify({ lyrics: saved.lyrics, source: saved.source, correction: saved.correction });
      useLyrics(saved.lyrics, saved.source, saved.correction, false);
    }
  }

  function detachVideo() {
    if (!state.video) return;
    state.video.removeEventListener('play', handleVideoPlay);
    state.video.removeEventListener('pause', syncPlayButton);
    state.video.removeEventListener('loadedmetadata', fillDefaultRange);
    state.video.removeEventListener('timeupdate', monitorPlayback);
    state.video = null;
    updatePlaybackMonitoring();
  }

  function watchPage() {
    const pageUrl = `${location.origin}${location.pathname}${location.search}`;
    if (pageUrl !== state.lastUrl) {
      state.lastUrl = pageUrl;
      detachVideo();
      loadForPage();
    }

    if (!state.video || !state.video.isConnected) {
      const candidate = findBestVideo();
      if (candidate) attachVideo(candidate);
      else {
        el.videoStatus.classList.remove(`${P}connected`);
        el.videoStatus.querySelector('span').textContent = '正在寻找播放器…';
      }
    }
  }

  function scheduleVideoScan(delay = 150) {
    clearTimeout(state.videoScanTimer);
    state.videoScanTimer = setTimeout(watchPage, delay);
  }

  function onKeydown(event) {
    if (state.collapsed || state.view !== 'practice' || isEditable(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    if (!['r', 'arrowleft', 'arrowright', 'enter', ' '].includes(key)) return;
    event.preventDefault();
    if (key === 'r') replayCurrent();
    else if (key === 'arrowleft') selectSentence(state.activeIndex - 1, true);
    else if (key === 'arrowright') selectSentence(state.activeIndex + 1, true);
    else if (key === 'enter') markMastered();
    else if (key === ' ') togglePlay();
  }

  buildPanel();
  loadForPage();
  watchPage();
  setInterval(() => {
    const pageUrl = `${location.origin}${location.pathname}${location.search}`;
    if (pageUrl !== state.lastUrl || !state.video || !state.video.isConnected) watchPage();
  }, 2000);
  document.addEventListener('keydown', onKeydown, true);
  document.addEventListener('visibilitychange', updatePlaybackMonitoring);
  window.addEventListener('pagehide', () => {
    const jobId = state.activeJobId;
    const pageKey = state.pageKey;
    if (jobId) cancelJob(jobId, pageKey, 1);
  });
  new MutationObserver(() => {
    if (!state.video || !state.video.isConnected) scheduleVideoScan();
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
