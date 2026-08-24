(() => {
  'use strict';

  const shared = window.BLSShared;

  function storageKey(pageKey) {
    return `${shared.STORAGE_PREFIX}${pageKey}`;
  }

  function validLine(line) {
    const start = Number(line?.start);
    const end = Number(line?.end);
    const text = String(line?.text || '').trim();
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || !text) return null;
    return { start, end, text, mastered: line.mastered === true };
  }

  function validateSnapshot(value) {
    if (!value || value.schemaVersion !== shared.STORAGE_SCHEMA_VERSION || !Array.isArray(value.lyrics)) return null;
    const lyrics = value.lyrics.map(validLine).filter(Boolean);
    if (!lyrics.length) return null;
    for (let index = 1; index < lyrics.length; index += 1) {
      if (lyrics[index].start < lyrics[index - 1].start || lyrics[index - 1].end > lyrics[index].start) return null;
    }
    return {
      schemaVersion: shared.STORAGE_SCHEMA_VERSION,
      lyrics,
      source: String(value.source || '已保存歌词').slice(0, 200),
      correction: value.correction && typeof value.correction === 'object' && !Array.isArray(value.correction)
        ? { ...value.correction }
        : null,
      savedAt: Number.isFinite(Number(value.savedAt)) ? Number(value.savedAt) : 0
    };
  }

  async function load(pageKey) {
    if (!pageKey) return null;
    try {
      const key = storageKey(pageKey);
      const result = await chrome.storage.local.get(key);
      return validateSnapshot(result?.[key]);
    } catch (error) {
      console.warn('声迹：读取本地缓存失败', error);
      return null;
    }
  }

  let writeQueue = Promise.resolve();

  function save(pageKey, snapshot) {
    const validated = validateSnapshot({
      ...snapshot,
      schemaVersion: shared.STORAGE_SCHEMA_VERSION,
      savedAt: Date.now()
    });
    if (!pageKey || !validated) return Promise.resolve(false);

    writeQueue = writeQueue.then(async () => {
      try {
        const all = await chrome.storage.local.get(null);
        const entries = Object.entries(all)
          .filter(([key]) => key.startsWith(shared.STORAGE_PREFIX))
          .map(([key, value]) => ({ key, savedAt: Number(value?.savedAt) || 0 }))
          .sort((a, b) => b.savedAt - a.savedAt);
        const key = storageKey(pageKey);
        const staleKeys = entries
          .filter(entry => entry.key !== key)
          .slice(shared.MAX_STORAGE_ENTRIES - 1)
          .map(entry => entry.key);
        await chrome.storage.local.set({ [key]: validated });
        if (staleKeys.length) await chrome.storage.local.remove(staleKeys);
        return true;
      } catch (error) {
        console.warn('声迹：保存本地缓存失败', error);
        return false;
      }
    });
    return writeQueue;
  }

  window.BLSStorage = Object.freeze({ load, save });
})();
