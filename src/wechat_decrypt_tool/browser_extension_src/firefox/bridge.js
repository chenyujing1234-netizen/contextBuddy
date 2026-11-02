// 隔离环境桥接脚本（chrome/ 与 firefox/ 两目录内容保持一致）
// 作用：content.js 运行在页面主环境（world: MAIN），无法直接调用扩展 API；
//      本脚本运行在扩展隔离环境（ISOLATED），负责在「页面主环境 ⇄ 扩展后台」之间搬运消息。
// 消息类型（payload.type，缺省为 gm-xhr）：
//   gm-xhr          —— 代发跨域请求（Word 转换服务 api.any2card.com）
//   ext-get-settings —— 读取面板设置（popup 存入 chrome.storage.local）
//   ext-download     —— 经后台 chrome.downloads 把内容保存到「下载目录/{saveDir}/」
(function () {
    'use strict';

    window.addEventListener('message', function (event) {
        if (event.source !== window || !event.data || event.data.__aiExportBridgeRequest !== true) return;

        var reqId = event.data.reqId;
        var payload = event.data.payload || {};
        var msgType = payload.type || 'gm-xhr';

        try {
            chrome.runtime.sendMessage({ type: msgType, payload: payload }, function (result) {
                var reply = (result && typeof result === 'object')
                    ? result
                    : { ok: false, status: 0, statusText: '扩展后台无响应' };
                window.postMessage({ __aiExportBridgeReply: reqId, result: reply }, '*');
            });
        } catch (e) {
            // 扩展上下文失效（如刚重新加载过扩展）等情况
            window.postMessage({
                __aiExportBridgeReply: reqId,
                result: { ok: false, status: 0, statusText: '桥接失败: ' + (e && e.message ? e.message : e) }
            }, '*');
        }
    });
})();
