// 扩展后台（chrome/ 与 firefox/ 两目录内容保持一致）
// 职责：
//   1. gm-xhr          —— 代理跨域请求（Word 转换服务 api.any2card.com），返回值以 base64 回传
//   2. ext-get-settings —— 读取面板设置（popup 写入 chrome.storage.local，content 经桥接读取）
//   3. ext-get-device-id —— 读取本机设备标识（首次自动生成并持久化，跨站点共享，
//                          上传的会话数据归属此标识，服务端下载接口按它隔离数据）
//   4. ext-download     —— 经 chrome.downloads 把 base64 内容保存到「下载目录/{saveDir}/」
//      目录仅允许浏览器默认下载文件夹下的相对子目录（可多级，自动创建），不允许绝对路径/..
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg) return;

    // 面板设置读取
    if (msg.type === 'ext-get-settings') {
        chrome.storage.local.get(['exportSettings'], function (r) {
            sendResponse({ ok: true, settings: r.exportSettings || null });
        });
        return true; // 异步 sendResponse，保持消息通道开启
    }

    // 设备标识读取（首次生成 'dev-' + UUID 并持久化，同一浏览器所有平台站点共享）
    if (msg.type === 'ext-get-device-id') {
        chrome.storage.local.get(['deviceId'], function (r) {
            if (r && r.deviceId) {
                sendResponse({ ok: true, deviceId: r.deviceId });
                return;
            }
            var id = 'dev-' + ((typeof crypto !== 'undefined' && crypto.randomUUID)
                ? crypto.randomUUID()
                : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12));
            chrome.storage.local.set({ deviceId: id }, function () {
                sendResponse({ ok: true, deviceId: id });
            });
        });
        return true;
    }

    // 自动保存：base64 内容 → data URL → chrome.downloads
    if (msg.type === 'ext-download') {
        var p = msg.payload || {};
        if (!p.base64 || !p.filename) {
            sendResponse({ ok: false, error: '缺少文件内容或文件名' });
            return true;
        }
        var dir = String(p.saveDir || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        var parts = dir.split('/').filter(function (s) { return s && s !== '.' && s !== '..'; });
        var filename = (parts.length ? parts.join('/') + '/' : '') + String(p.filename);
        chrome.downloads.download({
            url: 'data:' + (p.mime || 'application/octet-stream') + ';base64,' + p.base64,
            filename: filename,
            saveAs: false,
            conflictAction: 'uniquify'
        }, function (downloadId) {
            if (chrome.runtime.lastError || downloadId === undefined) {
                sendResponse({ ok: false, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : '下载失败' });
            } else {
                sendResponse({ ok: true, id: downloadId });
            }
        });
        return true;
    }

    if (msg.type !== 'gm-xhr' || !msg.payload || !msg.payload.url) return;

    var p = msg.payload;
    fetch(p.url, {
        method: p.method || 'GET',
        headers: p.headers || {},
        body: p.data || undefined
    }).then(function (resp) {
        if (resp.ok) {
            return resp.blob().then(function (blob) {
                return blobToBase64(blob).then(function (base64) {
                    sendResponse({
                        ok: true,
                        status: resp.status,
                        statusText: resp.statusText,
                        base64: base64,
                        contentType: blob.type
                    });
                });
            });
        }
        // 非 2xx：与篡改猴行为一致，仍走 onload，把错误响应文本带回
        return resp.text().catch(function () { return ''; }).then(function (errorText) {
            sendResponse({
                ok: true,
                status: resp.status,
                statusText: resp.statusText,
                errorText: errorText
            });
        });
    }).catch(function (err) {
        sendResponse({
            ok: false,
            status: 0,
            statusText: (err && err.message) ? err.message : String(err)
        });
    });

    return true; // 异步 sendResponse，保持消息通道开启
});

// Blob → base64（分块拼接，兼容 service worker 环境，不依赖 FileReader）
function blobToBase64(blob) {
    return blob.arrayBuffer().then(function (buf) {
        var bytes = new Uint8Array(buf);
        var binary = '';
        var chunkSize = 0x8000;
        for (var i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
        }
        return btoa(binary);
    });
}
