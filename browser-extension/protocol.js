(() => {
  'use strict';

  const config = globalThis.BLSConfig || {};
  const apiBase = String(config.API_BASE || '').replace(/\/+$/, '');
  if (!apiBase) throw new Error('缺少 API_BASE 配置，请使用 build-release.ps1 生成扩展包。');

  globalThis.BLSShared = Object.freeze({
    STORAGE_PREFIX: 'bls-data:',
    STORAGE_SCHEMA_VERSION: 1,
    MAX_STORAGE_ENTRIES: 50,
    MAX_RANGE_SECONDS: 15 * 60,
    MIN_LINE_DURATION: 0.1,
    API_BASE: apiBase,
    TOKEN_STORAGE_KEY: 'singReactorApiToken',
    ACTIVE_JOBS_STORAGE_KEY: 'bls-active-jobs',
    JOB_POLL_INTERVAL_MS: 1200,
    JOB_POLL_MAX_RETRIES: 5,
    JOB_POLL_MAX_DELAY_MS: 10000,
    nextPollDelay(failures, base = 1200, maximum = 10000) {
      return Math.min(maximum, base * (2 ** Math.max(0, failures - 1)));
    },
    shouldRetryJobOperation(response, failures, maximum = 5) {
      return !response?.ok && failures < maximum;
    },
    JOB_TERMINAL_STATUSES: Object.freeze(['succeeded', 'failed', 'cancelled']),
    MESSAGE_TYPES: Object.freeze({
      FETCH_SUBTITLES: 'fetchBilibiliSubtitles',
      CREATE_TRANSCRIBE_JOB: 'createTranscribeJob',
      GET_TRANSCRIBE_JOB: 'getTranscribeJob',
      CANCEL_TRANSCRIBE_JOB: 'cancelTranscribeJob'
    })
  });
})();
