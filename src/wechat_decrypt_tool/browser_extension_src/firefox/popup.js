// 面板设置读写：写入 chrome.storage.local，页面里的 content.js 经后台桥接读取。
// 首次使用需在此点一次「保存设置」才会启用自动保存（未保存过设置时页面端视为未配置，保持原有手动下载行为）。
const $ = (id) => document.getElementById(id);
const DEFAULTS = { autoSave: true, saveDir: 'AI对话导出', formats: { json: true, md: true, word: true } };

chrome.storage.local.get(['exportSettings'], (r) => {
    const saved = (r && r.exportSettings) || {};
    const s = Object.assign({}, DEFAULTS, saved);
    s.formats = Object.assign({}, DEFAULTS.formats, saved.formats || {});
    $('autoSave').checked = !!s.autoSave;
    $('saveDir').value = s.saveDir || '';
    $('fmtJson').checked = !!s.formats.json;
    $('fmtMd').checked = !!s.formats.md;
    $('fmtWord').checked = !!s.formats.word;
});

$('save').addEventListener('click', () => {
    const settings = {
        autoSave: $('autoSave').checked,
        saveDir: $('saveDir').value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, ''),
        formats: {
            json: $('fmtJson').checked,
            md: $('fmtMd').checked,
            word: $('fmtWord').checked
        }
    };
    chrome.storage.local.set({ exportSettings: settings }, () => {
        $('status').textContent = '已保存 ✓ 刷新已打开的页面后生效';
        setTimeout(() => { $('status').textContent = ''; }, 3000);
    });
});
