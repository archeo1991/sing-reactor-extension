'use strict';

importScripts('config.js');
importScripts('protocol.js');

const BILIBILI_API = 'https://api.bilibili.com';
const shared = globalThis.BLSShared;
const MESSAGE_TYPES = shared.MESSAGE_TYPES;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !Object.values(MESSAGE_TYPES).includes(message.type)) return;

  let task;
  if (message.type === MESSAGE_TYPES.FETCH_SUBTITLES) task = fetchBilibiliSubtitles(message.url);
  else if (message.type === MESSAGE_TYPES.CREATE_TRANSCRIBE_JOB) task = createTranscribeJob(message.payload);
  else if (message.type === MESSAGE_TYPES.GET_TRANSCRIBE_JOB) task = getTranscribeJob(message.jobId);
  else if (message.type === MESSAGE_TYPES.CANCEL_TRANSCRIBE_JOB) task = cancelTranscribeJob(message.jobId);
  else return;

  task.then(data => sendResponse({ ok: true, ...data }))
    .catch(error => sendResponse({ ok: false, error: friendlyError(error) }));
  return true;
});

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { credentials: 'include', ...options });
  if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
  try {
    return await response.json();
  } catch (_) {
    throw new Error('响应不是有效 JSON');
  }
}

async function fetchBilibiliSubtitles(pageUrl) {
  const url = new URL(pageUrl);
  const bvid = url.pathname.match(/\/video\/(BV[0-9A-Za-z]+)/i)?.[1];
  if (!bvid) throw new Error('当前页面未找到 BV 号。番剧页面可能无法通过公开视频接口取得字幕，可改用声迹服务识别。');

  const view = await fetchJson(`${BILIBILI_API}/x/web-interface/view?bvid=${encodeURIComponent(bvid)}`);
  checkBilibili(view, '读取视频信息');
  const pages = Array.isArray(view.data?.pages) ? view.data.pages : [];
  const requestedPage = Math.max(1, Number.parseInt(url.searchParams.get('p') || '1', 10) || 1);
  const page = pages.find(item => Number(item.page) === requestedPage) || pages[requestedPage - 1];
  const cid = page?.cid || view.data?.cid;
  if (!cid) throw new Error(`未找到第 ${requestedPage}P 的 cid，可改用声迹服务识别。`);

  const player = await fetchJson(`${BILIBILI_API}/x/player/v2?bvid=${encodeURIComponent(bvid)}&cid=${encodeURIComponent(cid)}`);
  checkBilibili(player, '读取字幕列表');
  const subtitles = Array.isArray(player.data?.subtitle?.subtitles) ? player.data.subtitle.subtitles : [];
  if (!subtitles.length) return { found: false, reason: '这个视频没有可用的 B站字幕，可使用声迹服务识别。' };

  const selected = subtitles.find(isChineseSubtitle) || subtitles[0];
  const subtitle = await fetchJson(normalizeSubtitleUrl(selected.subtitle_url), { credentials: 'omit' });
  const body = Array.isArray(subtitle.body) ? subtitle.body : [];
  const lyrics = body.map(item => ({
    start: Number(item.from), end: Number(item.to), text: String(item.content || '').trim(), mastered: false
  })).filter(item => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start && item.text);
  if (!lyrics.length) throw new Error('字幕资源中没有有效句子，可改用声迹服务识别。');
  return { found: true, lyrics, source: `B站字幕 · ${selected.lan_doc || selected.lan || '默认语言'}`, bvid, cid };
}

function checkBilibili(payload, action) {
  if (!payload || payload.code !== 0) {
    const detail = payload?.message || payload?.msg || '未知错误';
    throw new Error(`${action}失败：${detail}（code ${payload?.code ?? '未知'}）`);
  }
}

function isChineseSubtitle(item) {
  const value = `${item?.lan || ''} ${item?.lan_doc || ''}`.toLowerCase();
  return /(^|[\s_-])(zh|chi|cn|中文|汉语|繁体|简体)/i.test(value) || /中文|汉语|简体|繁体/.test(value);
}

function normalizeSubtitleUrl(value) {
  if (!value) throw new Error('字幕地址为空');
  if (value.startsWith('//')) return `https:${value}`;
  return new URL(value, 'https://www.bilibili.com').href;
}

function buildLocalHeaders(token) {
  const headers = { 'Content-Type': 'application/json' };
  const value = typeof token === 'string' ? token.trim() : '';
  if (value) headers['X-Sing-Reactor-Token'] = value;
  return headers;
}

let memorySessionToken = '';

async function clearSessionToken() {
  memorySessionToken = '';
  const area = chrome.storage.session;
  if (!area) return;
  if (area.remove) {
    try {
      await area.remove(shared.TOKEN_STORAGE_KEY);
      return;
    } catch (_) {
      // 回退到写入空字符串。
    }
  }
  try {
    await area.set?.({ [shared.TOKEN_STORAGE_KEY]: '' });
  } catch (_) {
    // storage.session 不可用时内存 token 已清除。
  }
}

async function readStorageToken(area) {
  if (!area?.get) return '';
  try {
    const stored = await area.get(shared.TOKEN_STORAGE_KEY);
    return typeof stored?.[shared.TOKEN_STORAGE_KEY] === 'string'
      ? stored[shared.TOKEN_STORAGE_KEY].trim()
      : '';
  } catch (_) {
    return '';
  }
}

async function getLocalApiToken() {
  const configuredToken = await readStorageToken(chrome.storage.local);
  if (configuredToken) return configuredToken;

  const sessionToken = await readStorageToken(chrome.storage.session);
  if (sessionToken) {
    memorySessionToken = sessionToken;
    return sessionToken;
  }
  if (memorySessionToken) return memorySessionToken;

  const response = await fetch(`${shared.API_BASE}/auth/session`, {
    method: 'GET',
    cache: 'no-store',
    headers: { 'X-Sing-Reactor-Extension': chrome.runtime.id }
  });
  if (!response.ok) throw new Error(`无法获取服务会话 token（HTTP ${response.status}）。`);
  const result = await response.json();
  const token = typeof result?.token === 'string' ? result.token.trim() : '';
  if (!token) throw new Error('服务返回了无效会话 token。');
  memorySessionToken = token;
  try {
    await chrome.storage.session?.set?.({ [shared.TOKEN_STORAGE_KEY]: token });
  } catch (_) {
    // storage.session 不可用时仅保留在 service worker 内存中。
  }
  return token;
}

globalThis.BLSBackgroundLogic = Object.freeze({
  buildLocalHeaders,
  getLocalApiToken,
  localApi,
  nextPollDelay: shared.nextPollDelay,
  shouldRetryJobOperation: shared.shouldRetryJobOperation
});

async function localApi(path, options = {}) {
  let token;
  let usesConfiguredToken = false;
  try {
    const configuredToken = await readStorageToken(chrome.storage.local);
    usesConfiguredToken = Boolean(configuredToken);
    token = configuredToken || await getLocalApiToken();
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error('无法连接声迹服务。请稍后重试，或联系服务维护者。');
    }
    throw error;
  }

  const request = async requestToken => {
    try {
      return await fetch(`${shared.API_BASE}${path}`, {
        ...options,
        headers: { ...buildLocalHeaders(requestToken), ...(options.headers || {}) }
      });
    } catch (_) {
      throw new Error('无法连接声迹服务。请稍后重试，或联系服务维护者。');
    }
  };

  let response = await request(token);
  if (response.status === 401 && !usesConfiguredToken) {
    await clearSessionToken();
    try {
      token = await getLocalApiToken();
    } catch (error) {
      if (error instanceof TypeError) {
        throw new Error('无法连接声迹服务。请稍后重试，或联系服务维护者。');
      }
      throw error;
    }
    response = await request(token);
  }

  let result;
  try {
    result = await response.json();
  } catch (_) {
    throw new Error(`声迹服务返回了无效响应（HTTP ${response.status}）。`);
  }
  if (!response.ok) {
    const detail = typeof result?.detail === 'string' ? result.detail : JSON.stringify(result?.detail || result);
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return result;
}

function createTranscribeJob(payload) {
  const body = {
    url: payload?.url,
    start: Number(payload?.start),
    end: Number(payload?.end),
    language: payload?.language || 'auto'
  };
  return localApi('/transcribe/jobs', { method: 'POST', body: JSON.stringify(body) });
}

function getTranscribeJob(jobId) {
  if (!jobId) throw new Error('识别任务 ID 为空');
  return localApi(`/transcribe/jobs/${encodeURIComponent(jobId)}`, { method: 'GET' });
}

function cancelTranscribeJob(jobId) {
  if (!jobId) return Promise.resolve({ status: 'cancelled' });
  return localApi(`/transcribe/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
}

function friendlyError(error) {
  const message = error instanceof Error ? error.message : String(error || '未知错误');
  if (/Failed to fetch|NetworkError|fetch/i.test(message)) return `网络请求失败：${message}`;
  return message;
}
