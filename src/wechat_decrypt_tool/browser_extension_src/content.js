/*
 * AI对话导出 word/json/md —— 浏览器扩展版 content script（页面主环境）
 * 本文件由 build-extension.js 从 scrollmerge.js 自动生成，请勿手工修改；
 * 修改逻辑请编辑源 scrollmerge.js，然后重新运行: node extension/build-extension.js
 * 生成时间: 2026-10-23T07:23:34.489Z
 */

(function() {
    'use strict';

    // ============ 浏览器扩展兼容层（替代篡改猴的 GM_* API） ============
    // 本段由 build-extension.js 自动插入。本文件运行在页面主环境（world: MAIN）：
    // GM_addStyle 直接注入样式；GM_xmlhttpRequest 经 bridge.js（隔离环境）转发到
    // background.js（扩展后台）完成跨域请求，响应字段与篡改猴保持一致。

    function GM_addStyle(css) {
        const style = document.createElement('style');
        style.textContent = css;
        (document.head || document.documentElement).appendChild(style);
        return style;
    }

    function GM_xmlhttpRequest(opts) {
        const reqId = 'gmxhr_' + Date.now() + '_' + Math.random().toString(36).slice(2);
        const timer = setTimeout(() => {
            window.removeEventListener('message', onBridgeReply);
            if (opts.onerror) opts.onerror({ status: 0, statusText: '请求超时' });
        }, 120000);

        function onBridgeReply(event) {
            if (event.source !== window || !event.data || event.data.__aiExportBridgeReply !== reqId) return;
            clearTimeout(timer);
            window.removeEventListener('message', onBridgeReply);
            const r = event.data.result || {};
            let response = null;
            try {
                if (typeof r.base64 === 'string') {
                    // 2xx：后台回传文件 base64，还原为 Blob（与篡改猴 responseType:blob 行为一致）
                    const bin = atob(r.base64);
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    response = new Blob([bytes], { type: r.contentType || 'application/octet-stream' });
                } else if (typeof r.errorText === 'string') {
                    // 非 2xx：后台回传错误响应文本，同样交给 onload 处理（与篡改猴一致）
                    response = new Blob([r.errorText], { type: 'text/plain' });
                }
            } catch (e) {
                console.error('[AI对话导出] ERROR: 解析后台响应失败', e);
            }
            const details = { status: r.status || 0, statusText: r.statusText || '', response: response };
            if (r.ok) {
                if (opts.onload) opts.onload(details);
            } else if (opts.onerror) {
                opts.onerror(details);
            }
        }

        window.addEventListener('message', onBridgeReply);
        window.postMessage({
            __aiExportBridgeRequest: true,
            reqId: reqId,
            payload: {
                method: opts.method || 'GET',
                url: opts.url,
                headers: opts.headers || {},
                data: opts.data || null
            }
        }, '*');
    }
    console.log('🚀 脚本开始加载', new Date().toISOString());

    // 配置常量
    const CONFIG = {
        API_ENDPOINT_WORD: 'https://api.any2card.com/api/md-to-word',
        DEFAULT_TITLE: '未命名对话',
        BUTTON_OPACITY: 0.3,
        BUTTON_HOVER_OPACITY: 1,
        SPINNER_ANIMATION_DURATION: '1s',
        REL_AND_KNOWLEDGE: false, // 默认值为false，表示默认不导出参考链接和知识库
        // 数据库上传服务（接收插件数据写入 MySQL plugin_gpt_conversation 库）
        UPLOAD_SERVER: {
            url: 'http://114.55.254.123:8321',
            token: '525186097a4d4879266580c3c3e72cd9384d0eb50a1f52b7'
        }
    };

    // 平台URL模式
    const PLATFORM_PATTERNS = {
        deepseek: /chat_session_id=/,
        ncn: /conversation\/info\?conversation_id=/,
        yuanbao: /\/api\/user\/agent\/conversation\/v1\/detail/,
        kimi:  /(\/apiv2\/kimi\.chat\.v1\.ChatService\/ListMessages|\/api\/chat\/[^\/]+\/messages|\/api\/chat|chat_session_id)/,///\/apiv2\/kimi\.chat\.v1\.ChatService\/ListMessages/
        kimi_title: /\/apiv2\/kimi\.chat\.v1\.ChatService\/GetChat/,//kimi标题信息API
        tongyi: /\/dialog\/chat\/list/,
        // 千问（qianwen.com，原通义千问新版）：历史消息列表 + 会话信息（含标题）
        qianwen: /\/api\/v1\/session\/msg\/list/,
        qianwen_title: /\/api\/v1\/session\/get/,
        iflytek: /\/iflygpt\/u\/chat_history\/all\//,
        doubao:/\/im\/chain\/single/, //过期alice\/message\/list/
        doubao_title:/\/im\/conversation\/info/ // 豆包标题会话信息API
    };

    // 状态管理
    const state = {
        targetResponse: null,
        lastUpdateTime: null,
        convertedMd: null,
        platformType: null,
        messageStats: {
            totalTokens: 0,
            totalChars: 0,
            fileCount: 0,
            questions: 0,
            convTurns: 0
        },
        currentTitle: CONFIG.DEFAULT_TITLE,
        kimiSessionId: null,
        kimiTitleCache: null,
        qianwenSessionId: null,
        qianwenTitleCache: null,
        authToken: null,
        doubaoApiQuery: null,     // 豆包IM请求的完整query串（从真实请求动态捕获，批量导出重放时使用）
        doubaoBatchBusy: false,   // 豆包批量导出进行中：跳过重放的doubao响应，避免污染批量结果
        qianwenAuth: { deviceId: null, xsrf: null, userId: null }, // 千问鉴权头（从真实请求动态捕获）
        qianwenListQuery: null,   // 千问会话列表接口的query串模板（动态捕获）
        qianwenMsgQuery: null,    // 千问消息接口的query串模板（动态捕获）
        qianwenBatchBusy: false,  // 千问批量导出进行中：跳过重放的qianwen响应，避免污染批量结果
        deepseekAuth: { token: null, deviceId: null }, // DeepSeek鉴权头（Authorization + x-device-id，动态捕获）
        deepseekBatchBusy: false, // DeepSeek批量导出进行中：跳过重放的deepseek响应
        yuanbaoListReq: null,     // 元宝会话列表请求模板 {headers, body}（动态捕获，含鉴权头与body结构）
        yuanbaoDetailQuery: null, // 元宝消息接口的query串模板（含分页参数时动态学习）
        yuanbaoBatchBusy: false,  // 元宝批量导出进行中：跳过重放的yuanbao响应，且不更新捕获模板
        kimiBatchBusy: false,     // Kimi批量导出进行中：跳过重放的kimi响应
        iflytekListQuery: null,   // 讯飞星火会话列表接口请求模板（动态捕获）
        iflytekBatchBusy: false,  // 讯飞星火批量导出进行中：跳过重放的iflytek响应
        // 分页历史记录累积池：key为会话标识，map以消息ID去重存储所有已拦截的消息
        mergeStore: {
            key: null,
            map: new Map(),
            title: null
        }
    };

    // 日志工具
    const logger = {
        info: (msg, data) => console.log(`[AI对话导出] INFO: ${msg}`, data || ''),
        warn: (msg, data) => console.warn(`[AI对话导出] WARN: ${msg}`, data || ''),
        error: (msg, error) => console.error(`[AI对话导出] ERROR: ${msg}`, error || '')
    };

    // 脚本版本（与头部 @version 保持一致）：启动日志/批次上报都会携带，
    // 便于排查"浏览器里实际还在跑旧版本"类问题
    const SCRIPT_VERSION = '2026.10.23-3.scrollmerge';

    // 工具函数
    const utils = {
        formatTimestamp: (timestamp) => {
            if (!timestamp) return 'N/A';
            let dt;
            try {
                // 1. 检查是否是数字（Unix 时间戳） const ts = typeof timestamp === 'string' ? parseInt(timestamp) : timestamp;
                if (typeof timestamp === 'number') {
                    // 判断是秒级还是毫秒级（通常毫秒级时间戳 >= 1e12）
                    dt = new Date(timestamp < 1e12 ? timestamp * 1000 : timestamp);
                }
                // 2. 检查是否是字符串（纯数字时间戳 / ISO 8601 或日期字符串）
                else if (typeof timestamp === 'string') {
                    if (/^\d+(\.\d+)?$/.test(timestamp.trim())) {
                        // 纯数字字符串按Unix时间戳处理
                        const n = Number(timestamp);
                        dt = new Date(n < 1e12 ? n * 1000 : n);
                    } else {
                        // ISO 8601 / "yyyy-MM-dd HH:mm:ss" 等日期字符串直接解析
                        dt = new Date(timestamp);
                    }
                }
                // 3. 其他情况（如已经是 Date 对象）
                else {
                    dt = new Date(timestamp);
                }

                // 检查日期是否有效
                if (isNaN(dt.getTime())) {
                    return 'Invalid Date';
                }

                // 使用瑞典格式 (YYYY-MM-DD HH:MM:SS) 并替换特殊字符
                return dt.toLocaleString('sv').replace(/[T \/]/g, '_');
            } catch (e) {
                logger.error('时间戳格式化错误', e);
                return 'Invalid Date';
            }
        },

        adjustHeaderLevels: (text, increaseBy = 1) => {
            if (!text) return '';
            return text.replace(/^(#+)(\s*)(.*?)\s*$/gm, (match, hashes, space, content) => {
                return '#'.repeat(hashes.length + increaseBy) + ' ' + content.trim();
            });
        },

        getLocalTimestamp: () => {
            const d = new Date();
            const offset = d.getTimezoneOffset() * 60 * 1000;
            return new Date(d.getTime() - offset).toISOString()
                .slice(0, 19)
                .replace(/T/g, '_')
                .replace(/:/g, '-');
        },

        sanitizeFilename: (name) => {
            return name.replace(/[\/\\?%*:|"<>]/g, '-');
        },

        createBlobDownload: (content, type, filename) => {
            // 扩展环境且面板设置了保存目录：优先经后台 chrome.downloads 存入子目录，失败回退默认下载
            (async () => {
                try {
                    const st = await autoSaver.loadSettings();
                    if (st && st.saveDir) {
                        const ok = await autoSaver.saveViaBridge(st.saveDir, filename, content, type);
                        if (ok) return;
                        logger.warn(`保存到「下载/${st.saveDir}」失败，回退到浏览器默认下载`);
                    }
                } catch (e) { /* 回退到默认下载 */ }
                try {
                    const blob = content instanceof Blob ? content : new Blob([content], { type });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    setTimeout(() => {
                        document.body.removeChild(a);
                        URL.revokeObjectURL(url);
                    }, 100);
                } catch (e) {
                    logger.error('创建Blob下载失败', e);
                }
            })();
            return true;
        }
    };

    // ============ 分页历史记录合并（解决滚动加载的旧轮次丢失问题） ============
    // 各平台历史API是分页返回的：页面打开只拿到最近几轮，向上滚动才会请求更早的轮次。
    // 这里把每次拦截到的“一页”数据按消息ID去重合并到累积池，导出时使用完整数据。
    const MERGE_CONFIG = {
        deepseek: {
            getList: d => d?.data?.biz_data?.chat_messages,
            setList: (d, list) => { d.data.biz_data.chat_messages = list; },
            getId: m => m.message_id ?? m.id ?? m.inserted_at,
            sortKey: m => m.inserted_at ?? m.message_id ?? 0,
            getTitle: d => d?.data?.biz_data?.chat_session?.title,
            setTitle: (d, t) => { if (d?.data?.biz_data) { d.data.biz_data.chat_session = d.data.biz_data.chat_session || {}; d.data.biz_data.chat_session.title = t; } }
        },
        ncn: {
            getList: d => d?.data?.messages,
            setList: (d, list) => { d.data.messages = list; },
            getId: m => m.id ?? m.msg_id ?? m.qid ?? m.create_time ?? m.prompt,
            sortKey: m => m.create_time ?? m.id ?? 0,
            getTitle: d => d?.data?.title,
            setTitle: (d, t) => { if (d?.data) d.data.title = t; }
        },
        yuanbao: {
            getList: d => d?.convs,
            setList: (d, list) => { d.convs = list; },
            getId: m => m.id ?? `${m.index}|${m.speaker}`,
            sortKey: m => m.index ?? 0,
            getTitle: d => d?.sessionTitle || d?.title,
            setTitle: (d, t) => { d.sessionTitle = t; }
        },
        kimi: {
            getList: d => d?.messages,
            setList: (d, list) => { d.messages = list; },
            getId: m => m.id ?? m.messageId ?? m.createTime,
            sortKey: m => m.createTime ?? 0
        },
        tongyi: {
            getList: d => d?.data,
            setList: (d, list) => { d.data = list; },
            getId: m => m.msgId ?? m.id ?? m.createTime,
            sortKey: m => m.createTime ?? 0
        },
        qianwen: {
            // 千问每轮是 {req_id, pos, request_messages, response_messages}，pos为毫秒时间戳字符串
            getList: d => d?.data?.list,
            setList: (d, list) => { d.data.list = list; },
            getId: m => m.req_id ?? m.pos ?? m.request_timestamp,
            sortKey: m => Number(m.pos) || m.request_timestamp || 0
        },
        iflytek: {
            getList: d => d?.data?.[0]?.historyList,
            setList: (d, list) => { if (d?.data?.[0]) d.data[0].historyList = list; },
            getId: m => m.id ?? m.sid ?? m.createTime,
            sortKey: m => m.createTime ?? m.id ?? 0
        },
        doubao: {
            getList: d => d?.downlink_body?.pull_singe_chain_downlink_body?.messages,
            setList: (d, list) => { d.downlink_body.pull_singe_chain_downlink_body.messages = list; },
            getId: m => m.message_id ?? m.server_message_id ?? m.local_message_id ?? `${m.create_time}|${m.user_type}`,
            sortKey: m => m.create_time ?? 0
        }
    };

    // 比较排序键：数字直接比，字符串尝试按日期比，否则按字典序
    function compareSortKeys(a, b) {
        if (a == null || b == null) return 0;
        if (typeof a === 'number' && typeof b === 'number') return a - b;
        const da = Date.parse(a), db = Date.parse(b);
        if (!isNaN(da) && !isNaN(db)) return da - db;
        return String(a).localeCompare(String(b));
    }

    // 会话标识：切换到别的对话时清空累积池，避免混入上一个对话的内容
    function getConversationKey(platform, url) {
        const m = String(url).match(/(?:chat_session_id|conversation_id|sessionId|session_id|chat_id)=([^&]+)/);
        if (m) return `${platform}|${m[1]}`;
        return `${platform}|${location.pathname}${location.search}`;
    }

    function mergeHistoryData(platform, jsonData, url) {
        const cfg = MERGE_CONFIG[platform];
        if (!cfg) return jsonData;
        const newList = cfg.getList(jsonData);
        if (!Array.isArray(newList)) return jsonData;

        const convKey = getConversationKey(platform, url);
        if (state.mergeStore.key !== convKey) {
            state.mergeStore.key = convKey;
            state.mergeStore.map = new Map();
            state.mergeStore.title = null;
            logger.info(`检测到新会话，重置历史消息累积池: ${convKey}`);
        }

        newList.forEach(msg => {
            const id = cfg.getId(msg);
            if (id !== undefined && id !== null) {
                state.mergeStore.map.set(String(id), msg);
            }
        });

        const merged = [...state.mergeStore.map.values()]
            .sort((a, b) => compareSortKeys(cfg.sortKey(a), cfg.sortKey(b)));
        cfg.setList(jsonData, merged);

        // 分页响应里可能没有标题，沿用之前拿到的标题
        if (cfg.getTitle) {
            const t = cfg.getTitle(jsonData);
            if (t) {
                state.mergeStore.title = t;
            } else if (state.mergeStore.title && cfg.setTitle) {
                cfg.setTitle(jsonData, state.mergeStore.title);
            }
        }

        logger.info(`历史消息合并: 本次响应 ${newList.length} 条，累积共 ${merged.length} 条`);
        return jsonData;
    }
    // ============ 分页合并模块结束 ============

    // 平台特定的转换器
    const platformConverters = {
        deepseek: (data) => {
            let mdContent = [];
            // 1. 安全的数据访问
            const bizData = data?.data?.biz_data || {};
            const chatSession = bizData.chat_session || {};
            const chatMessages = bizData.chat_messages || [];

            const title = chatSession.title || CONFIG.DEFAULT_TITLE;
            const totalTokens = chatMessages.reduce((acc, msg) =>
                                                    acc + (msg.accumulated_token_usage || 0), 0);

            // 2. 标题和统计信息
            mdContent.push(`# DeepSeek对话 - ${title}`);
            mdContent.push(`\n> 统计信息：累计Token用量 ${totalTokens}`);
            mdContent.push(`> 生成时间：${new Date().toLocaleString()}\n`);

            // 3. 处理消息（用户提问和AI回答都导出）
            chatMessages.forEach((msg,index) => {
                const role = msg.role === 'ASSISTANT' ? '🤖 Assistant' : '👤 用户';
                const timestamp = utils.formatTimestamp(msg.inserted_at);

                mdContent.push(`## ${role}`);
                mdContent.push(`*${timestamp}*\n`);

                // 4. 处理文件信息
                if (msg.files?.length > 0) {
                    mdContent.push('### 文件信息');
                    msg.files.forEach(file => {
                        mdContent.push(`- 名称: ${file.file_name}`);
                        mdContent.push(`- 大小: ${(file.file_size / 1024).toFixed(1)}KB`);
                        mdContent.push(`- Token用量: ${file.token_usage}`);
                    });
                    mdContent.push('');
                }

                // 5. 处理消息片段（思考过程和回复内容）
                let content = '';
                let thinkingContent = '';

                if (msg.fragments?.length > 0) {
                    const citations = {};
                    msg.fragments.forEach(fragment => {
                        if (fragment.cite_index !== null) {
                            citations[fragment.cite_index] = fragment.url;
                        }

                        switch (fragment.type) {
                            case 'THINK':
                                thinkingContent += fragment.content + '\n';
                                break;
                            case 'RESPONSE':
                                content += fragment.content + '\n';
                                break;
                            case 'TIP':
                                content += `\n> 💡 ${fragment.content}\n`;
                                break;
                            case 'REQUEST':
                                content += fragment.content + '\n';
                                break;
                        }
                    });
                    content = content.replace(/\[citation:(\d+)\]/g, (match, p1) => {
                        const url = citations[parseInt(p1)];
                        return url ? ` [${p1}](${url})` : match;
                    });
                    // 重要的空格处理（避免公式中的空格问题）
                    content = content.replace(/\s+,/g, ',').replace(/\s+\./g, '.');
                }

                // 5.5 用户提问通常没有fragments，正文直接在content字段里
                if (!content.trim() && typeof msg.content === 'string' && msg.content.trim()) {
                    content = msg.content;
                }

                // 6. 添加思考过程（如果存在）
                if (thinkingContent.trim()) {
                    const thinkFragment = msg.fragments.find(f => f.type === 'THINK');
                    const thinkTime = thinkFragment?.elapsed_secs ? `(${thinkFragment.elapsed_secs.toFixed(1)}s)` : '';
                    content += `\n**思考过程 ${thinkTime}:**\n${thinkingContent.trim()}\n`;
                }

                // 7. 数学公式格式化 - 智能处理
                content = content.replace(/(\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|\$\$.*?\$\$)/g, (match) => {
                    // 提取公式内容（移除各种边界符号）
                    let formula = match.replace(/^\\\(|\\\)$|^\\\[|\\\]$|\$\$/g, '');

                    // 转换为单行
                    formula = formula.replace(/\s+/g, ' ').trim();

                    // 返回统一的 $...$ 格式
                    return `$${formula}$`;
                });

                mdContent.push(content.trim() + '\n');
                mdContent.push('---\n');
            });
            return mdContent.join('\n');
        },

        ncn: (data) => {
            let mdContent = [];
            const meta = data.data || {};
            mdContent.push(`# 纳米AI对话记录 - ${meta.title || CONFIG.DEFAULT_TITLE}`);
            mdContent.push(`**生成时间**: ${utils.formatTimestamp(data.timestamp) || '未知'}`);


            const totalChars = meta.messages?.reduce((acc, msg) =>
                acc + (msg.result?.length || 0), 0) || 0;
            const questions = meta.messages?.reduce((acc, msg) =>
                acc + (msg.ask_further?.length || 0), 0) || 0;

            mdContent.push(`\n> 统计信息：总字数 ${totalChars} | 后续问题 ${questions} 个`);

            meta.messages?.forEach(msg => {
                if (msg.file?.length) {
                    mdContent.push('### 附件信息');
                    msg.file.forEach(file => {
                        mdContent.push(`- ${file.title} (${(file.size / 1024).toFixed(1)}KB)`);
                    });
                }

                // 用户提问和AI回复
                mdContent.push(`\n## 用户提问\n${msg.prompt || '无内容'}`);
                // 处理AI回复内容，移除引用标记
                let cleanResult = msg.result || '无内容';
                // 移除类似[[3]()][[7]()]的引用标记
                cleanResult = cleanResult.replace(/\[\[\d+\]\(\)\]/g, '');
                // 移除多余的换行和空格
                cleanResult = cleanResult.replace(/\n{3,}/g, '\n').trim();
                mdContent.push(`\n## AI回复\n${cleanResult}`);
                //mdContent.push(`## AI回复\n${msg.result || '无内容'}`);

                // 推荐追问
                if (msg.ask_further?.length) {
                    mdContent.push('### 推荐追问');
                    msg.ask_further.forEach(q => {
                        mdContent.push(`- ${q.content}`);
                    });
                }

                //是否需要导出，通过全局配置进行选择
                if(CONFIG.REL_AND_KNOWLEDGE){
                    // 参考链接
                    if (msg.refer_search?.length) {
                        mdContent.push('\n### 参考来源');
                        msg.refer_search.forEach(ref => {
                            mdContent.push(`- [${ref.title || '无标题'}](${ref.url}) - ${ref.summary || '无摘要'}`);
                            if (ref.date) mdContent.push(`  - 发布日期: ${ref.date}`);
                            if (ref.site) mdContent.push(`  - 来源网站: ${ref.site}`);
                        });
                    }

                    // 用户知识库引用
                    if (msg.user_knowledge?.length) {
                        mdContent.push('\n### 知识库引用');
                        const uniqueFiles = new Map();
                        msg.user_knowledge.forEach(knowledge => {
                            if (knowledge.file_name && !uniqueFiles.has(knowledge.file_id)) {
                                uniqueFiles.set(knowledge.file_id, knowledge);
                                mdContent.push(`- ${knowledge.file_name} (来自文件夹: ${knowledge.folder_id || '未知'})`);
                            }
                        });
                    }
                }

                mdContent.push('\n---\n');
            });
            return mdContent.join('\n');
        },

        yuanbao: (data) => {
            if (!data?.convs || !Array.isArray(data.convs)) {
                logger.error('无效的元宝数据', data);
                return '# 错误：无效的JSON数据\n\n无法解析对话内容。';
            }

            let markdownContent = [];
            const title = data.sessionTitle || data.title || '元宝对话记录';
            markdownContent.push(`# ${title}\n`);

            if (data.multiMediaInfo?.length > 0) {
                markdownContent.push('**包含的多媒体文件:**\n');
                data.multiMediaInfo.forEach(media => {
                    markdownContent.push(`* [${media.fileName || '未知文件'}](${media.url || '#'}) (${media.type || '未知类型'})\n`);
                });
                markdownContent.push('---\n');
            }

            const sortedConvs = [...data.convs].sort((a, b) => (a.index || 0) - (b.index || 0));

            sortedConvs.forEach(turn => {
                const timestamp = utils.formatTimestamp(turn.createTime);
                const index = turn.index !== undefined ? turn.index : 'N/A';

                // 用户提问轮次
                if (turn.speaker === 'human') {
                    markdownContent.push(`\n## 👤 用户 (轮次 ${index})\n`);
                    markdownContent.push(`*时间: ${timestamp}*\n`);

                    let userText = '';
                    const collectBlocks = (blocks) => {
                        blocks?.forEach(block => {
                            if (block.type === 'text' && block.msg) {
                                userText += block.msg + '\n\n';
                            } else if (['image', 'code', 'pdf', 'file'].includes(block.type)) {
                                userText += `*附件:* [${block.fileName || '未知文件'}](${block.url || '#'}) (类型: ${block.type})\n\n`;
                            }
                        });
                    };

                    // 用户内容可能在 turn.content 数组里，也可能在 speechesV2 里
                    if (Array.isArray(turn.content)) {
                        collectBlocks(turn.content);
                    }
                    if (!userText.trim()) {
                        turn.speechesV2?.forEach(speech => collectBlocks(speech.content));
                    }
                    if (!userText.trim() && typeof turn.content === 'string') {
                        userText = turn.content;
                    }
                    if (!userText.trim() && typeof turn.displayPrompt === 'string') {
                        userText = turn.displayPrompt;
                    }

                    markdownContent.push((userText.trim() || '（无内容）') + '\n');
                    markdownContent.push('\n---\n');
                    return;
                }

                if (turn.speaker === 'ai') {
                    markdownContent.push(`\n## AI (轮次 ${index})\n`);

                    let modelDisplay = '未知模型';
                    let primaryPluginId = '无插件';

                    if (turn.speechesV2?.length > 0) {
                        const firstSpeech = turn.speechesV2[0];
                        const modelIdRaw = firstSpeech.chatModelId;
                        primaryPluginId = firstSpeech.pluginId || primaryPluginId;

                        if (modelIdRaw && String(modelIdRaw).trim() !== '') {
                            modelDisplay = `\`${modelIdRaw}\``;
                        }
                    }

                    markdownContent.push(`*时间: ${timestamp} | 模型: ${modelDisplay} | 插件: \`${primaryPluginId}\`*\n\n`);

                    turn.speechesV2?.forEach(speech => {
                        speech.content?.forEach(block => {
                            switch (block.type) {
                                case 'text':
                                    markdownContent.push(`${utils.adjustHeaderLevels(block.msg || '', 1)}\n\n`);
                                    break;
                                case 'think':
                                    markdownContent.push(`> **[思考过程]** ${block.title || ''}\n>\n`);
                                    (block.content || '无思考内容').split('\n').forEach(line => {
                                        markdownContent.push(`> ${line}\n`);
                                    });
                                    markdownContent.push('\n');
                                    break;
                                case 'searchGuid':
                                    markdownContent.push(`**${block.title || '搜索结果'}** (查询: \`${block.botPrompt || 'N/A'}\` | 主题: ${block.topic || 'N/A'})\n`);
                                    block.docs?.forEach((doc, docIndex) => {
                                        markdownContent.push(`* [${docIndex + 1}] [${doc.title || '无标题'}](${doc.url || '#'}) (${doc.sourceName || '未知来源'})\n    * > ${doc.quote || '无引用'}\n`);
                                    });
                                    markdownContent.push('\n');
                                    break;
                                case 'image':
                                case 'code':
                                case 'pdf':
                                    markdownContent.push(`*文件:* [${block.fileName || '未知文件'}](${block.url || '#'}) (类型: ${block.type})\n\n`);
                                    break;
                            }
                        });
                    });
                }
                markdownContent.push('\n---\n');
            });

            return markdownContent.join('\n').replace(/\n---\n$/, '').trim();
        },

        kimi: async (data, titleOverride) => {
            let mdContent = [];
            const title = titleOverride || await fetchKimiChatTitle() || 'Kimi对话记录';
            //const title = state.currentTitle || 'Kimi对话记录';
            mdContent.push(`# ${title}\n`);

            // 处理消息数组
            data.messages?.forEach((message, index) => {
                const role = message.role === 'user' ? '👤 用户' : '🤖 Kimi';
                const timestamp = utils.formatTimestamp(message.createTime);

                mdContent.push(`## ${role} - ${timestamp}\n`);

                // 处理blocks中的内容
                message.blocks?.forEach(block => {
                    if (block.text?.content) {
                        // 文本内容
                        mdContent.push(block.text.content + '\n');
                    } else if (block.think?.content) {
                        // 思考过程（新版Kimi的think块）
                        mdContent.push(`> **[思考过程]**\n> ${String(block.think.content).split('\n').join('\n> ')}\n\n`);
                    } else if (block.artifact) {
                        // 特殊内容（如PPT、文件等）
                        if (block.artifact.type === 'ARTIFACT_TYPE_SLIDES_JSON') {
                            mdContent.push('### 📊 生成的PPT内容\n');
                            try {
                                const slidesData = JSON.parse(block.artifact.content);
                                mdContent.push(`**标题:** ${slidesData.outline?.pptTitle || '无标题'}\n`);
                                mdContent.push(`**页数:** ${slidesData.outline?.slidesCount || 0}\n`);
                                // 可以进一步解析PPT内容
                            } catch (e) {
                                mdContent.push('*[PPT内容解析失败]*\n');
                            }
                        }
                    } else if (block.slidesView) {
                        // 幻灯片视图信息
                        mdContent.push(`### 🎨 幻灯片视图\n`);
                        mdContent.push(`- 名称: ${block.slidesView.name}\n`);
                        mdContent.push(`- 状态: ${block.slidesView.status}\n`);
                        if (block.slidesView.coverUrl) {
                            mdContent.push(`- 封面: ![封面](${block.slidesView.coverUrl})\n`);
                        }
                    }
                });

                mdContent.push('\n---\n');
            });

            return mdContent.join('\n');
        },

        tongyi: (data) => {
            let mdContent = [];
            mdContent.push(`# 通义千问对话记录`);

            const convTurns = data.data?.length || 0;
            const totalChars = data.data?.reduce((acc, msg) => {
                return acc + (msg.contents?.reduce((sum, content) => {
                    if (content.contentType === 'text' && content.content) {
                        return sum + content.content.length;
                    } else if (content.contentType === 'plugin' && content.content) {
                        // 计算插件内容中的文本长度
                        try {
                            const pluginData = JSON.parse(content.content);
                            if (pluginData.pluginCall) {
                                const callData = JSON.parse(pluginData.pluginCall);
                                return sum + (callData.prompt?.length || 0);
                            }
                        } catch (e) {
                            return sum;
                        }
                    }
                    return sum;
                }, 0) || 0);
            }, 0) || 0;

            mdContent.push(`\n> 统计信息：对话轮次 ${convTurns} | 总字数 ${totalChars}`);

            if (data.data?.length) {
                const sortedMessages = [...data.data].sort((a, b) => a.createTime - b.createTime);

                sortedMessages.forEach(msg => {
                    const time = utils.formatTimestamp ? utils.formatTimestamp(msg.createTime) :
                new Date(msg.createTime).toLocaleString();
            const role = msg.senderType === 'USER' ? '用户' : '助手';
            mdContent.push(`## ${role} (${time})\n`);

            msg.contents?.forEach(content => {
                if (content.content) {
                    if (content.contentType === 'text') {
                        // 处理普通文本
                        let text = content.content.replace(/\n/g, '\n\n');
                        text = text.replace(/```([\s\S]*?)```/g, '\n```$1```\n');
                        mdContent.push(text);
                    } else if (content.contentType === 'plugin') {
                        // 处理插件内容（文生图等）
                        try {
                            const pluginData = JSON.parse(content.content);
                            mdContent.push('### 插件调用信息');

                            if (pluginData.pluginCall) {
                                const callData = JSON.parse(pluginData.pluginCall);
                                mdContent.push(`**功能：** ${callData.toolName || '未知'}`);
                                mdContent.push(`**提示词：** ${callData.prompt || '无'}`);
                                mdContent.push(`**尺寸：** ${callData.size || '未指定'}`);
                            }

                            if (pluginData.pluginResult?.taskResult?.wanx_text_to_image?.imageList) {
                                const images = pluginData.pluginResult.taskResult.wanx_text_to_image.imageList;
                                mdContent.push('### 生成的图片');
                                images.forEach((img, index) => {
                                    mdContent.push(`**图片 ${index + 1}：**`);
                                    mdContent.push(`- 分辨率：${img.resolution}`);
                                    mdContent.push(`- 状态：${img.isSecurity ? '安全' : '待审核'}`);
                                    mdContent.push(`- 预览：![](${img.resizeUrl})`);
                                    mdContent.push(`- 原图：[点击下载](${img.url})`);
                                });
                            }

                            if (pluginData.pluginResult?.status) {
                                mdContent.push(`**任务状态：** ${pluginData.pluginResult.status}`);
                            }
                        } catch (e) {
                            mdContent.push('`[插件内容解析错误]`');
                        }
                    }
                }
            });
            mdContent.push('\n---\n');
        });
    } else {
        mdContent.push('\n暂无对话内容');
    }
            return mdContent.join('\n');
        },

        // 千问消息转换（titleOverride：批量导出时传入各会话自己的标题）
        qianwen: (data, titleOverride) => {
            let mdContent = [];
            mdContent.push(`# ${titleOverride || '通义千问对话记录'}`);

            const turns = data.data?.list || [];
            const totalChars = turns.reduce((acc, turn) => {
                const q = (turn.request_messages || []).reduce(
                    (s, m) => s + (m.mime_type === 'text/plain' ? String(m.content || '').length : 0), 0);
                const a = (turn.response_messages || []).reduce(
                    (s, m) => s + (m.mime_type === 'multi_load/iframe' && m.content ? String(m.content).length : 0), 0);
                return acc + q + a;
            }, 0);
            mdContent.push(`\n> 统计信息：对话轮次 ${turns.length} | 总字数 ${totalChars}`);

            if (turns.length) {
                const sortedTurns = [...turns].sort((a, b) =>
                    (Number(a.pos) || a.request_timestamp || 0) - (Number(b.pos) || b.request_timestamp || 0));

                sortedTurns.forEach(turn => {
                    const time = utils.formatTimestamp(Number(turn.pos) || turn.request_timestamp);

                    // 用户提问（文本 + 图片等附件）
                    const reqParts = [];
                    (turn.request_messages || []).forEach(m => {
                        if (m.mime_type === 'text/plain' && m.content) {
                            reqParts.push(String(m.content));
                        } else if (m.mime_type === 'image/url') {
                            try {
                                const res = JSON.parse(m.meta_data?.resource_infos || '[]');
                                const names = (Array.isArray(res) ? res : [])
                                    .map(r => r.file_name || r.url || r.id).filter(Boolean);
                                reqParts.push(`[附件图片: ${names.join(', ') || '图片'}]`);
                            } catch (e) {
                                reqParts.push('[附件图片]');
                            }
                        }
                    });
                    if (reqParts.length) {
                        mdContent.push(`## 👤 用户 (${time})\n`);
                        reqParts.forEach(p => mdContent.push(p));
                        mdContent.push('');
                    }

                    // AI 回答：正文在 multi_load/iframe 类型消息的 content 里（其余类型为进度条/信号等UI事件，无正文）
                    const answers = (turn.response_messages || [])
                        .filter(m => m.mime_type === 'multi_load/iframe' && m.content && String(m.content).trim())
                        .map(m => String(m.content));
                    if (answers.length) {
                        mdContent.push(`## 🤖 助手 (${time})\n`);
                        answers.forEach(a => mdContent.push(a));
                        mdContent.push('');
                    }

                    mdContent.push('\n---\n');
                });
            } else {
                mdContent.push('\n暂无对话内容');
            }
            return mdContent.join('\n');
        },

        iflytek: (data, titleOverride) => {
            let mdContent = [];
            mdContent.push(`# ${titleOverride || '讯飞星火对话记录'}`);

            const convTurns = data.data?.[0]?.historyList?.length || 0;
            const totalChars = data.data?.[0]?.historyList?.reduce((acc, msg) => {
                return acc + (msg.message?.length || 0) + (msg.answer?.length || 0);
            }, 0) || 0;

            mdContent.push(`\n> 统计信息：对话轮次 ${convTurns} | 总字数 ${totalChars}`);

            data.data?.[0]?.historyList?.forEach(msg => {
                if (msg.type === 0) {
                    const time = utils.formatTimestamp(msg.createTime);
                    mdContent.push(`## 用户 (${time})\n\n${msg.message}\n`);
                } else if (msg.message) {
                    const time = utils.formatTimestamp(msg.createTime);
                    let sourceInfo = '';

                    if (msg.traceSource) {
                        try {
                            const sources = JSON.parse(msg.traceSource);
                            if (Array.isArray(sources)) {
                                sources.forEach(source => {
                                    if (source.type === 'searchSource' && source.data) {
                                        sourceInfo += '\n\n**参考来源**:\n';
                                        source.data.forEach(item => {
                                            sourceInfo += `- [${item.docid}](${item.source})\n`;
                                        });
                                    }
                                });
                            }
                        } catch (e) {
                            logger.error('解析来源信息失败', e);
                        }
                    }

                    mdContent.push(`## AI助手 (${time})\n\n${msg.message}${sourceInfo}\n`);
                }
                mdContent.push('---\n');
            });

            return mdContent.join('\n');
        },
        // 豆包消息转换（titleOverride：批量导出时传入各会话自己的标题）
        doubao: (data, titleOverride) => {
            let mdContent = [];
            //const title = data.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name||'豆包对话记录';
            const title = titleOverride || state.currentTitle || '豆包对话记录';

            // 获取正确的消息数组
            const messages = data.downlink_body?.pull_singe_chain_downlink_body?.messages || [];

            mdContent.push(`# ${title}\n`);

            // 统计信息
            const totalMessages = messages.length;
            let totalChars = 0;

            // 处理消息内容 - 按时间正序排列
            const sortedMessages = [...messages].sort((a, b) => a.create_time - b.create_time);

            sortedMessages.forEach(msg => {
                const time = utils.formatTimestamp(msg.create_time);
                const role = msg.user_type === 1 ? '用户' : '豆包AI';

                mdContent.push(`## ${role} (${time})`);

                // 处理消息内容
                let content = '';
                let messageChars = 0;

                try {
                    if (msg.content_type === 1) { // 用户文本消息
                        const contentObj = typeof msg.content === 'string' ? JSON.parse(msg.content || '{}') : msg.content;
                        content = contentObj.text || JSON.stringify(contentObj) || '';
                        messageChars = content.length;

                    } else if (msg.content_type === 9999) { // AI复合消息
                        // 方法1: 解析content字段（JSON数组）
                        if (msg.content) {
                            const contentArray = typeof msg.content === 'string' ? JSON.parse(msg.content || '[]') : msg.content;
                            if (Array.isArray(contentArray)) {
                                contentArray.forEach(item => {
                                    if (item.block_type === 10000 && item.content?.text_block?.text) {
                                        // 文本块
                                        content += item.content.text_block.text + '\n\n';
                                        messageChars += item.content.text_block.text.length;
                                    } else if (item.block_type === 2074 && item.content?.creation_block) {
                                        // 图片块
                                        const mediaContent = platformConverters.processDoubaoMediaContent(item);
                                        content += mediaContent + '\n\n';
                                    }
                                });
                            }
                        }

                        // 方法2: 使用content_block（如果存在）
                        if (!content.trim() && msg.content_block?.length) {
                            msg.content_block.forEach(block => {
                                if (block.block_type === 10000 && block.content?.text_block?.text) {
                                    content += block.content.text_block.text + '\n\n';
                                    messageChars += block.content.text_block.text.length;
                                } else if (block.block_type === 2074) {
                                    const mediaContent = platformConverters.processDoubaoMediaContent(block);
                                    content += mediaContent + '\n\n';
                                }
                            });
                        }
                    }

                    totalChars += messageChars;

                } catch (e) {
                    console.error('解析消息内容失败', e);
                    content = `解析错误: ${e.message}`;
                }

                // 清理内容格式
                content = content.replace(/```([\s\S]*?)```/g, '\n```$1```\n')
                    .replace(/\n{3,}/g, '\n\n')
                    .trim();

                mdContent.push(content || '（无内容）');
                mdContent.push('\n---\n');
            });

            // 插入统计信息到合适位置
            if (mdContent.length > 1) {
                mdContent.splice(1, 0, `> 统计信息：消息数 ${totalMessages} | 总字数 ${totalChars}\n`);
            }

            return mdContent.join('\n');
        },

        // 处理豆包的媒体内容 - 修正版本
        processDoubaoMediaContent: (blockOrMsg) => {
            let content = [];

            try {
                let contentObj;
                if (blockOrMsg.content && typeof blockOrMsg.content === 'string') {
                    contentObj = JSON.parse(blockOrMsg.content);
                } else {
                    contentObj = blockOrMsg.content || {};
                }

                // 从creation_block或直接获取creations
                const creations = contentObj.creation_block?.creations || contentObj.creations || [];

                creations.forEach(creation => {
                    if (creation.type === 1 && creation.image) {
                        const img = creation.image;

                        // 获取图片URL（按优先级）
                        const imageUrl = img.image_ori_raw?.url || img.image_raw?.url ||
                              img.image_ori?.url || img.image_preview?.url;

                        if (imageUrl) {
                            content.push(`![${img.description || 'AI生成图片'}](${imageUrl})`);

                            // 添加生成信息
                            if (img.gen_params?.prompt) {
                                content.push(`**提示词:** ${img.gen_params.prompt}`);
                            }
                        }
                    }
                });
            } catch (e) {
                console.error('处理媒体内容失败', e);
                content.push('*[图片内容解析失败]*');
            }

            return content.join('\n\n');
        } ,
        // 豆包聊天列表转换器
        doubao_chat: (data) => {
            let mdContent = [];
            mdContent.push(`# 豆包对话列表\n`);

            // 统计信息
            const totalChats = data.data?.length || 0;
            mdContent.push(`> 统计信息：共 ${totalChats} 个对话\n`);

            // 处理对话列表
            data.data?.forEach(chat => {
                const time = utils.formatTimestamp(chat.create_time);
                mdContent.push(`## ${chat.title || '未命名对话'} (${time})`);
                mdContent.push(`- 对话ID: ${chat.conversation_id}`);
                mdContent.push(`- 最后更新时间: ${new Date(chat.update_time * 1000).toLocaleString()}`);
                mdContent.push(`- 消息数: ${chat.message_count || 0}`);
                mdContent.push('\n---\n');
            });

            return mdContent.join('\n');
        }

    };

    // 核心处理函数
    async function processTargetResponse(text, url) {
        try {
            //console.log('=== 开始处理响应 ===');
            //console.log('URL:', url);
            //console.log('响应长度:', text.length);

            let detectedPlatform = null;
            for (const [platform, pattern] of Object.entries(PLATFORM_PATTERNS)) {
                if (pattern.test(url)) {
                    detectedPlatform = platform;
                    break;
                }
            }
            //console.log(detectedPlatform);

            if (!detectedPlatform) {
                console.log('未匹配到任何平台，退出处理');
                return;
            }

            // 批量导出进行中（豆包/千问/DeepSeek/元宝/Kimi/星火）：重放的请求会经过自身的fetch/XHR钩子，
            // 跳过处理避免覆盖批量导出的合并结果
            if (state.doubaoBatchBusy && (detectedPlatform === 'doubao' || detectedPlatform === 'doubao_title')) {
                return;
            }
            if (state.qianwenBatchBusy && (detectedPlatform === 'qianwen' || detectedPlatform === 'qianwen_title')) {
                return;
            }
            if (state.deepseekBatchBusy && detectedPlatform === 'deepseek') {
                return;
            }
            if (state.yuanbaoBatchBusy && detectedPlatform === 'yuanbao') {
                return;
            }
            if (state.kimiBatchBusy && (detectedPlatform === 'kimi' || detectedPlatform === 'kimi_title')) {
                return;
            }
            if (state.iflytekBatchBusy && detectedPlatform === 'iflytek') {
                return;
            }

/*              if (detectedPlatform==='kimi') {
                text = await fetchkimiMessage();

            } */

            state.platformType = detectedPlatform;
            state.lastUpdateTime = new Date().toLocaleTimeString();

            // 重置统计信息
            state.messageStats = {
                totalTokens: 0,
                totalChars: 0,
                fileCount: 0,
                questions: 0,
                convTurns: 0
            };

            // 千问会话信息API（session/get）：仅提取标题，不覆盖已捕获的消息数据
            if (detectedPlatform === 'qianwen_title') {
                try {
                    const j = JSON.parse(text);
                    if (j.data?.title && j.data?.session_id) {
                        state.qianwenTitleCache = { sid: j.data.session_id, title: j.data.title };
                        // 已知当前会话时，仅当会话匹配才更新标题，避免侧边栏操作其他会话时串标题
                        if (!state.qianwenSessionId || state.qianwenSessionId === j.data.session_id) {
                            state.currentTitle = j.data.title;
                        }
                        logger.info(`更新千问会话标题: ${j.data.title}`);
                    }
                } catch (e) {
                    logger.error('解析千问标题响应失败', e);
                }
                return;
            }

            // 标题/会话列表类响应（豆包标题、Kimi标题、豆包会话列表）：只更新标题，
            // 不覆盖已捕获的消息数据（否则 targetResponse 会被标题 JSON 污染，导致导出/上传异常）
            if (detectedPlatform === 'kimi_title' || detectedPlatform === 'doubao_title' || detectedPlatform === 'doubao_chat') {
                try {
                    const tj = JSON.parse(text);
                    if (detectedPlatform === 'kimi_title') {
                        state.currentTitle = tj.chat?.name || state.currentTitle;
                        logger.info(`更新kimi标题: ${state.currentTitle}`);
                    } else if (detectedPlatform === 'doubao_title') {
                        state.currentTitle = tj.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name || '豆包对话';
                        logger.info(`更新豆包标题: ${state.currentTitle}`);
                    } else {
                        state.currentTitle = '豆包对话列表';
                    }
                } catch (e) { /* 标题解析失败忽略，保留旧标题 */ }
                return;
            }

            // 关键改动：不再用本次响应直接覆盖旧数据，
            // 而是把分页返回的消息合并进累积池，得到完整历史后再保存
            const jsonData = mergeHistoryData(detectedPlatform, JSON.parse(text), url);
            state.targetResponse = JSON.stringify(jsonData);
            const match = url.match(/(\/apiv2\/kimi\.chat\.v1\.ChatService\/ListMessages|\/api\/chat\/[^\/]+\/messages|\/api\/chat|chat_session_id)/);//Kimi标题

            // 平台特定处理
            switch(detectedPlatform) {
                case 'deepseek':
                    state.convertedMd = platformConverters.deepseek(jsonData);
                    state.messageStats.totalTokens = jsonData.data?.biz_data?.chat_messages?.reduce(
                        (acc, msg) => acc + (msg.accumulated_token_usage || 0), 0) || 0;
                    state.currentTitle = jsonData.data?.biz_data?.chat_session?.title || 'deepseek-Chat';
                    break;

                case 'ncn':
                    state.convertedMd = platformConverters.ncn(jsonData);
                    state.messageStats.totalChars = jsonData.data?.messages?.reduce(
                        (acc, msg) => acc + (msg.result?.length || 0), 0) || 0;
                    state.messageStats.questions = jsonData.data?.messages?.reduce(
                        (acc, msg) => acc + (msg.ask_further?.length || 0), 0) || 0;
                    state.currentTitle = jsonData.data?.title || 'AI-Chat';
                    break;

                case 'yuanbao':
                    state.convertedMd = platformConverters.yuanbao(jsonData);
                    state.messageStats.convTurns = jsonData.convs?.length || 0;
                    state.currentTitle = jsonData.sessionTitle || jsonData.title || 'Yuanbao-Chat';
                    state.messageStats.fileCount = jsonData.multiMediaInfo?.length || 0;
                    break;

                case 'kimi':
                case 'kimi_title':
                    if (match?.[1]) {
                        state.kimiSessionId = match[1];
                        state.currentTitle = await fetchKimiChatTitle();
                        //console.log('测试Kimi_case');
                    }
                     //console.log('测试Kimicase_title');
                    // 如果是标题API，只更新标题不处理内容
                    if (state.platformType === 'kimi_title') {
                        state.currentTitle = jsonData.chat?.name || 'kimi对话';
                        logger.info(`更新kimi标题: ${state.currentTitle}`);
                        return; // 标题API不需要处理消息内容
                    }
                    state.convertedMd = await platformConverters.kimi(jsonData);
                    state.messageStats.totalChars = jsonData.messages?.reduce(
                        (acc, item) => acc + (JSON.stringify(item.contents).length || 0), 0) || 0;
                    break;

                case 'tongyi':
                    state.convertedMd = platformConverters.tongyi(jsonData);
                    state.messageStats.convTurns = jsonData.data?.length || 0;
                    state.messageStats.totalChars = jsonData.data?.reduce((acc, msg) => {
                        return acc + (msg.contents?.reduce(
                            (sum, content) => sum + (content.content?.length || 0), 0) || 0);
                    }, 0) || 0;
                    state.currentTitle = '通义千问对话';
                    break;

                case 'qianwen': {
                    // 从请求URL提取会话ID（msg/list 的 session_id 在查询参数里）
                    const sidMatch = url.match(/[?&]session_id=([^&]+)/);
                    if (sidMatch) state.qianwenSessionId = sidMatch[1];
                    state.convertedMd = platformConverters.qianwen(jsonData);
                    state.messageStats.convTurns = jsonData.data?.list?.length || 0;
                    state.messageStats.totalChars = jsonData.data?.list?.reduce((acc, turn) => {
                        const q = (turn.request_messages || []).reduce(
                            (s, m) => s + (m.mime_type === 'text/plain' ? String(m.content || '').length : 0), 0);
                        const a = (turn.response_messages || []).reduce(
                            (s, m) => s + (m.mime_type === 'multi_load/iframe' && m.content ? String(m.content).length : 0), 0);
                        return acc + q + a;
                    }, 0) || 0;
                    // 标题来自 session/get 的缓存；缓存不属于本会话时用默认值
                    state.currentTitle = (state.qianwenTitleCache && state.qianwenTitleCache.sid === state.qianwenSessionId)
                        ? state.qianwenTitleCache.title : '通义千问对话';
                    break;
                }

                case 'iflytek':
                    state.convertedMd = platformConverters.iflytek(jsonData);
                    state.messageStats.convTurns = jsonData.data?.[0]?.historyList?.length || 0;
                    state.messageStats.totalChars = jsonData.data?.[0]?.historyList?.reduce((acc, msg) => {
                        return acc + (msg.message?.length || 0) + (msg.answer?.length || 0);
                    }, 0) || 0;
                    state.currentTitle = '讯飞星火对话';
                    break;
                case 'doubao':
                case 'doubao_title':
                    // 如果是标题API，只更新标题不处理内容
                    if (state.platformType === 'doubao_title') {
                        state.currentTitle = jsonData.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name || '豆包对话';
                        logger.info(`更新豆包标题: ${state.currentTitle}`);
                        return; // 标题API不需要处理消息内容
                    }

                    // 正常处理对话内容
                    state.convertedMd = platformConverters.doubao(jsonData);
                    state.messageStats.totalChars = jsonData.downlink_body?.pull_singe_chain_downlink_body?.messages?.reduce((acc, msg) => {
                        try {
                            const contentObj = JSON.parse(msg.content || '{}');
                            return acc + (contentObj.text?.length || 0);
                        } catch {
                            return acc + (msg.content?.length || 0);
                        }
                    }, 0) || 0;
                    state.messageStats.convTurns = jsonData.downlink_body?.pull_singe_chain_downlink_body?.messages?.length || 0;

                    // 如果当前标题还是默认值，尝试从对话数据中获取
                    if (state.currentTitle === CONFIG.DEFAULT_TITLE || state.currentTitle === '豆包对话') {
                        state.currentTitle = jsonData.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name || '豆包对话';
                    }
                    break;
            }

            ui.updateButtonStatus();
            logger.info(`成功处理${detectedPlatform.toUpperCase()}响应`);
        } catch (e) {
            logger.error('响应处理错误', e);
        }
    }

    // Kimi相关功能
    // sessionId提取
    function getKimiSessionId() {
        const currentUrl = window.location.href;
        // 匹配多种可能的URL格式
        const match = currentUrl.match(/(?:chat|chat_session)\/([a-zA-Z0-9-]+)(?:\/|$)/);
        return match ? match[1] : null;
    }

    // 拦截XHR请求获取授权令牌
    function setupAuthInterceptor() {
        const originalOpen = XMLHttpRequest.prototype.open;
        const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

        XMLHttpRequest.prototype.open = function(method, url) {
            this._requestURL = url;
            if (url.includes('/api/chat/') && !url.includes('/api/chat/list')) {
                logger.info(`拦截到Kimi API请求: ${method} ${url}`);
                this._isChatAPI = true;
            }
            return originalOpen.apply(this, arguments);
        };

        XMLHttpRequest.prototype.setRequestHeader = function(header, value) {
            if (this._isChatAPI && header.toLowerCase() === 'authorization') {
                state.authToken = value;
                logger.info('获取到Authorization头');
            }
            return originalSetRequestHeader.apply(this, arguments);
        };
    }

    // 获取Kimi对话标题
    async function fetchKimiChatTitle() {
        if (state.kimiTitleCache) return state.kimiTitleCache;
        //console.log('Kimi标题获取测试');

        state.kimiSessionId = getKimiSessionId();
        if (!state.kimiSessionId) {
            logger.warn('无法获取sessionId');
            return "Kimi对话";
        }

        // 等待获取授权令牌
        let retry = 0;
        while (!state.authToken && retry < 5) {
            logger.info('等待获取授权令牌...');
            await new Promise(resolve => setTimeout(resolve, 500));
            retry++;
        }

        if (!state.authToken) {
            logger.warn('未能获取授权令牌');
            return "Kimi对话";
        }

        try {
            const url = `https://www.kimi.com/api/chat/${state.kimiSessionId}`;
            logger.info(`请求标题API: ${url}`);

            const response = await fetch(url, {
                headers: {
                    'Authorization': state.authToken,
                    'Content-Type': 'application/json',
                },
                credentials: 'include'
            });

            if (!response.ok) {
                logger.warn(`API错误: ${response.status}`);
                return "Kimi对话";
            }

            const data = await response.json();
            logger.info('标题_API响应类型:', typeof data);
            logger.info('API响应:', data);

            //const kimidata =await fetchkimiMessage();
            //console.log('标题里面输出对话：',kimidata);

            state.kimiTitleCache = data.name || "Kimi对话";
            return state.kimiTitleCache;
        } catch (error) {
            logger.error('获取标题失败:', error);
            return "Kimi对话";
        }
    }

    // 获取Kimi对话信息
    async function fetchkimiMessage() {
        if (state.kimiTitleCache) return state.kimiTitleCache;

        state.kimiSessionId = getKimiSessionId();
        if (!state.kimiSessionId) {
            logger.warn('无法获取sessionId');
            return "Kimi信息";
        }

        // 等待获取授权令牌
        let retry = 0;
        while (!state.authToken && retry < 5) {
            logger.info('等待获取授权令牌...');
            await new Promise(resolve => setTimeout(resolve, 500));
            retry++;
        }

        if (!state.authToken) {
            logger.warn('未能获取授权令牌');
            return "Kimi信息";
        }

        try {
            const url = `https://www.kimi.com/apiv2/kimi.chat.v1.ChatService/ListMessages`;
            logger.info(`请求信息API: ${url}`);

            // 修正：使用POST方法并添加请求体
            const requestBody = {
                chat_id: state.kimiSessionId, // 需要传递chatId参数
                // 可能还需要其他参数，如：
                // limit: 100,
                // cursor: ""
            };

            const response = await fetch(url, {
                method: 'POST', // 改为POST方法
                headers: {
                    'Authorization': state.authToken,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(requestBody), // 添加请求体
                mode: 'cors',
                referrer: window.location.href, // 使用当前页面作为referrer
                credentials: 'include'
            });

            if (!response.ok) {
                logger.warn(`API错误: ${response.status}`);
                return "Kimi信息";
            }

            // 检查Content-Type确认是JSON
            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                const rawText = await response.text();
                console.warn('响应不是JSON:', rawText.substring(0, 200));
                return null;
            }

            const data = await response.json();
            //const data = JSON.stringify(restext, null, 2)
            logger.info('Kimi_API响应类型:', typeof data);
            logger.info('kimi_API响应:', data);
            //console.log('kimi_API响应:', JSON.stringify(data, null, 2));

            //state.kimiTitleCache = data.name || "Kimi对话";
            return data;

        } catch (error) {
            logger.error('获取kimi信息失败:', error);
            return "Kimi信息";
        }
    }

    // 导出功能
    // 提取获取文件名的公共逻辑
    function getExportFilename(extension = '') {
        const jsonData = JSON.parse(state.targetResponse || '{}');
        let platformPrefix = '';
        let defaultSuffix = 'Chat';

        switch(state.platformType) {
            case 'deepseek':
                platformPrefix = 'DeepSeek';
                defaultSuffix = 'deepseek-Chat';
                break;
            case 'ncn':
                platformPrefix = 'AI-N';
                defaultSuffix = 'AI-Chat';
                break;
            case 'yuanbao':
                platformPrefix = 'Yuanbao';
                defaultSuffix = 'Yuanbao-Chat';
                break;
            case 'kimi':
                platformPrefix = 'Kimi';
                defaultSuffix = 'Kimi-Chat';
                break;
            case 'tongyi':
                platformPrefix = 'Tongyi';
                defaultSuffix = 'Tongyi-Chat';
                break;
            case 'qianwen':
                platformPrefix = 'Qianwen';
                defaultSuffix = 'Qianwen-Chat';
                break;
            case 'iflytek':
                platformPrefix = 'Iflytek';
                defaultSuffix = 'Iflytek-Chat';
                break;
            case 'doubao':
            case 'doubao_title':
                platformPrefix = 'Doubao';
                defaultSuffix = state.platformType === 'doubao_chat' ? 'ChatList' : 'Chat';
                break;
            default:
                platformPrefix = 'AI';
                defaultSuffix = 'Chat';
        }

        const title = state.currentTitle ||
              jsonData.data?.biz_data?.chat_session?.title ||
              jsonData.data?.title ||
              jsonData.sessionTitle ||jsonData.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name||'豆包对话'||(state.platformType === 'doubao_chat' ? '豆包列表' : '豆包其他')||
              defaultSuffix;

        const sanitizedTitle = utils.sanitizeFilename(`${platformPrefix}_${title}`);
        return `${sanitizedTitle}_${utils.getLocalTimestamp()}${extension ? '.' + extension : ''}`;
    }

    // ============ 扩展版自动保存（扩展面板设置目录/格式，拉取完成后经后台自动下载） ============
    // 油猴版没有面板/桥接：loadSettings 返回 false，全部行为退化为原手动下载流程
    const autoSaver = {
        settings: undefined, // undefined=未加载；false=非扩展环境或未配置；对象=面板设置

        // 经 bridge.js（隔离环境）→ background.js（后台）转发消息（协议与 GM_xmlhttpRequest 兼容层一致）
        bridgeRequest: (payload, timeoutMs = 4000) => new Promise(resolve => {
            const reqId = 'autosave_' + Date.now() + '_' + Math.random().toString(36).slice(2);
            const timer = setTimeout(() => {
                window.removeEventListener('message', onReply);
                resolve(null);
            }, timeoutMs);
            function onReply(event) {
                if (event.source !== window || !event.data || event.data.__aiExportBridgeReply !== reqId) return;
                clearTimeout(timer);
                window.removeEventListener('message', onReply);
                resolve(event.data.result || null);
            }
            window.addEventListener('message', onReply);
            window.postMessage({ __aiExportBridgeRequest: true, reqId: reqId, payload: payload }, '*');
        }),

        loadSettings: async function() {
            if (this.settings !== undefined) return this.settings;
            try {
                const r = await this.bridgeRequest({ type: 'ext-get-settings' }, 1500);
                this.settings = (r && r.ok && r.settings && typeof r.settings === 'object') ? r.settings : false;
            } catch (e) {
                this.settings = false;
            }
            return this.settings;
        },

        blobToBase64: (blob) => blob.arrayBuffer().then(buf => {
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            return btoa(binary);
        }),

        // 经后台 chrome.downloads 保存到「浏览器下载目录/{saveDir}/{filename}」
        saveViaBridge: async function(saveDir, filename, content, mime) {
            const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
            const base64 = await this.blobToBase64(blob);
            const r = await this.bridgeRequest({
                type: 'ext-download',
                saveDir: saveDir || '',
                filename: filename,
                base64: base64,
                mime: blob.type || mime
            }, 60000);
            return !!(r && r.ok);
        },

        // Word：复用在线转换服务，成功后同样经后台保存
        saveWord: function(saveDir) {
            return new Promise((resolve, reject) => {
                const fileName = getExportFilename('docx');
                const chatName = fileName.replace(/_[\d_-]+\.docx$/, '');
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: CONFIG.API_ENDPOINT_WORD,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify({ markdown: state.convertedMd, title: chatName }),
                    responseType: 'blob',
                    onload: async (response) => {
                        if (response.status < 200 || response.status >= 300) {
                            reject(new Error(`转换服务返回 ${response.status}`));
                            return;
                        }
                        try {
                            const ok = await this.saveViaBridge(saveDir, fileName, response.response,
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
                            ok ? resolve(fileName) : reject(new Error('后台保存失败'));
                        } catch (e) { reject(e); }
                    },
                    onerror: (r) => reject(new Error(r.statusText || '无法连接转换服务'))
                });
            });
        },

        // 数据就绪后按面板设置自动保存（批量导出完成 / 加载全部完成时触发）
        autoSaveIfNeeded: async function(triggerLabel) {
            const st = await this.loadSettings();
            if (!st || !st.autoSave) return;
            const formats = st.formats || {};
            const dirNote = st.saveDir ? `下载/${st.saveDir}` : '下载目录';
            const saved = [];
            try {
                if (formats.json && state.targetResponse) {
                    const fn = getExportFilename('json');
                    if (await this.saveViaBridge(st.saveDir, fn, state.targetResponse, 'application/json')) saved.push(fn);
                }
                if (formats.md && state.convertedMd) {
                    const fn = getExportFilename('md');
                    if (await this.saveViaBridge(st.saveDir, fn, state.convertedMd, 'text/markdown')) saved.push(fn);
                }
            } catch (e) {
                logger.error('自动保存出错', e);
            }
            if (formats.word && state.convertedMd) {
                try {
                    saved.push(await this.saveWord(st.saveDir));
                } catch (e) {
                    logger.warn(`自动保存 Word 失败: ${e.message}`);
                }
            }
            if (saved.length) {
                logger.info(`自动保存完成（${triggerLabel}）: ${saved.length} 个文件 → ${dirNote}\n${saved.join('\n')}`);
            } else {
                logger.info(`自动保存跳过（${triggerLabel}）: 没有可保存的数据或未勾选任何格式`);
            }
        }
    };

    // ============ 数据库上传模块 ============
    // 把当前已捕获的数据（单会话或批量导出的全部会话）上传到 MySQL 接收服务

    // 右下角轻提示（自动任务的完成通知，不弹窗打断用户）
    function showToast(text, ms) {
        try {
            const d = document.createElement('div');
            d.textContent = text;
            d.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:999999;max-width:340px;' +
                'padding:10px 14px;border-radius:8px;background:rgba(40,40,40,.92);color:#fff;font-size:13px;' +
                'line-height:1.5;box-shadow:0 4px 12px rgba(0,0,0,.3);word-break:break-all;';
            document.body.appendChild(d);
            setTimeout(() => d.remove(), ms || 4000);
        } catch (e) {}
    }

    const uploader = {
        running: false,
        stopRequested: false,
        fetching: false, // 正在自动拉取全部会话
        deviceId: '', // 本机设备标识(懒加载)，上传数据归属此标识，服务端按它隔离可下载的数据

        // 获取设备标识(同一浏览器跨站点保持一致)：
        //   1. 扩展版：经后台 chrome.storage 持久化(首次自动生成)——所有平台站点共享
        //   2. 油猴版：GM 存储——同脚本所有站点共享
        //   3. 兜底：localStorage(按站点隔离，仅单站点内一致)
        getDeviceId: async function() {
            if (this.deviceId) return this.deviceId;
            const gen = () => 'dev-' + (crypto.randomUUID ? crypto.randomUUID()
                : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12));
            try {
                const r = await autoSaver.bridgeRequest({ type: 'ext-get-device-id' }, 1500);
                if (r && r.ok && r.deviceId) { this.deviceId = r.deviceId; return this.deviceId; }
            } catch (e) { /* 非扩展环境 */ }
            try {
                if (typeof GM_getValue === 'function') {
                    let id = GM_getValue('aiExportDeviceId');
                    if (!id) {
                        id = gen();
                        if (typeof GM_setValue === 'function') GM_setValue('aiExportDeviceId', id);
                    }
                    this.deviceId = id; return this.deviceId;
                }
            } catch (e) { /* ignore */ }
            try {
                let id = localStorage.getItem('aiExportDeviceId');
                if (!id) {
                    id = gen();
                    localStorage.setItem('aiExportDeviceId', id);
                }
                this.deviceId = id; return this.deviceId;
            } catch (e) { return ''; }
        },

        // 从页面 URL 提取当前会话 ID（仅单会话上传需要；批量上传的会话 ID 由数据自带）
        getConversationId: function() {
            const h = location.hostname;
            const p = location.pathname + location.search;
            let m;
            if (h.includes('deepseek')) {
                m = p.match(/\/a\/([0-9a-zA-Z-]+)/);
            } else if (h.includes('yuanbao') || h.includes('kimi') || h.includes('doubao')) {
                m = p.match(/\/chat\/([^/?#]+)/);
            } else if (h.includes('qianwen') || h.includes('tongyi')) {
                return state.qianwenSessionId || undefined;
            } else if (h.includes('n.cn')) {
                m = p.match(/[?&]conversation_id=([^&]+)/) || p.match(/\/chat\/([^/?#]+)/);
            }
            // 讯飞星火的数据自带 chatId，服务端直接读取，无需从 URL 提取
            return m ? decodeURIComponent(m[1]) : undefined;
        },

        // 统一 POST：油猴版用 GM_xmlhttpRequest；扩展版由 SHIM 经后台代理跨域
        httpPost: function(path, bodyObj) {
            return new Promise((resolve, reject) => {
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: CONFIG.UPLOAD_SERVER.url + path,
                    headers: { 'Content-Type': 'application/json', 'X-Upload-Token': CONFIG.UPLOAD_SERVER.token },
                    data: JSON.stringify(bodyObj),
                    timeout: 180000,
                    onload: async (r) => {
                        let text = r.response;
                        if (text instanceof Blob) text = await text.text(); // 扩展版 SHIM 回传 Blob
                        try {
                            resolve({ status: r.status, json: JSON.parse(text) });
                        } catch (e) {
                            resolve({ status: r.status, json: { ok: false, error: '响应解析失败: ' + String(text).slice(0, 120) } });
                        }
                    },
                    onerror: (r) => reject(new Error((r && r.statusText) || '无法连接上传服务')),
                    ontimeout: () => reject(new Error('上传超时'))
                });
            });
        },

        // 上传一个会话
        uploadOne: async function(platform, conversation, ctx) {
            try {
                const r = await this.httpPost('/upload', {
                    platform: platform,
                    conversation: conversation,
                    conversation_id: ctx.conversation_id,
                    title: ctx.title,
                    batch_type: ctx.batch_type,
                    device_id: this.deviceId || undefined // 数据归属的设备标识，服务端按它隔离可下载数据
                });
                if (r.status === 200 && r.json && r.json.ok) return { ok: true, count: r.json.message_count || 0, skipped: !!r.json.skipped };
                return { ok: false, error: (r.json && r.json.error) || ('HTTP ' + r.status) };
            } catch (e) {
                return { ok: false, error: e.message };
            }
        },

        // 主入口：可选"全部会话"（六平台自动拉取+增量上传）或仅当前会话
        // auto=true 时为每日自动上传模式：不弹确认框、失败不弹窗、完成改用右下角 toast 提示；
        // 自动模式返回任务结果：'ok'=已上传 / 'empty'=昨天无匹配会话 / 'retry'=环境未就绪或临时失败(下次页面加载重试)
        start: async function(button, auto) {
            if (this.running) {
                if (auto) return 'retry'; // 自动任务不与手动操作抢跑道，下次页面加载再试
                if (this.fetching) {
                    alert('正在拉取全部会话（进度见「📚 全部会话」按钮），如需中止请点该按钮。');
                    return;
                }
                this.stopRequested = true;
                if (button) button.innerText = '⏹ 停止中…';
                return;
            }

            const originalText = '⇧ 上传';
            const setBtn = (t, color) => {
                if (button) { button.innerText = t; button.style.backgroundColor = color || '#6f42c1'; }
            };

            // 六平台支持一键上传"全部会话"：先自动拉取左侧"最近"列表，再增量上传
            const h = location.hostname;
            const batchPlatform = h.includes('doubao') ? 'doubao'
                : h.includes('qianwen') ? 'qianwen'
                : h.includes('deepseek') ? 'deepseek'
                : h.includes('yuanbao') ? 'yuanbao'
                : h.includes('kimi') ? 'kimi'
                : h.includes('xfyun') ? 'iflytek'
                : null;
            let exporter = null;
            if (batchPlatform === 'doubao' && typeof doubaoBatchExporter !== 'undefined') exporter = doubaoBatchExporter;
            else if (batchPlatform === 'qianwen' && typeof qianwenBatchExporter !== 'undefined') exporter = qianwenBatchExporter;
            else if (batchPlatform === 'deepseek' && typeof deepseekBatchExporter !== 'undefined') exporter = deepseekBatchExporter;
            else if (batchPlatform === 'yuanbao' && typeof yuanbaoBatchExporter !== 'undefined') exporter = yuanbaoBatchExporter;
            else if (batchPlatform === 'kimi' && typeof kimiBatchExporter !== 'undefined') exporter = kimiBatchExporter;
            else if (batchPlatform === 'iflytek' && typeof iflytekBatchExporter !== 'undefined') exporter = iflytekBatchExporter;

            let fetchAll = false;
            if (auto) {
                // 每日自动上传：必须有批量导出器，且只走"全部会话"链路
                if (!exporter) return 'retry';
                fetchAll = true;
            } else if (exporter) {
                fetchAll = confirm('「⇧ 上传到数据库」\n\n【确定】上传全部会话：自动拉取左侧"最近"列表的全部会话并增量上传（已上传且无变化的自动跳过）\n【取消】仅上传当前打开的会话');
            }
            if (!fetchAll && !state.targetResponse) {
                alert('还没有可上传的对话数据。\n请先打开一个对话等待数据捕获，或选择"全部会话"。');
                return;
            }

            this.running = true;
            this.stopRequested = false;
            setBtn('⇧ 上传中…', '#dc3545');

            // 懒加载设备标识：数据归属此标识，服务端只允许下载该标识上传的数据
            this.deviceId = await this.getDeviceId();

            // 自动模式：暂存并清空当前捕获状态，防止旧的 targetResponse 被误当作本次结果上传；
            // 结束后恢复，不影响用户手动导出/上传当前会话
            if (auto) {
                this._autoSaved = {
                    targetResponse: state.targetResponse,
                    platformType: state.platformType,
                    currentTitle: state.currentTitle,
                    messageStats: state.messageStats,
                    convertedMd: state.convertedMd
                };
                state.targetResponse = null;
            }

            try {
                // 自动拉取全部会话（进度显示在「📚 全部会话」按钮上）
                if (fetchAll) {
                    this.fetching = true;
                    setBtn('⇧ 拉取全部会话…', '#dc3545');
                    // 极端情况下按钮尚未创建（如自动任务早于 UI 初始化）时用占位对象兜底，避免报错
                    const allBtn = document.getElementById('downloadAllSessionsButton') ||
                        { style: {}, innerText: '' };
                    try {
                        await exporter.start(allBtn, auto ? { filter: this._autoFilter, silent: true } : undefined);
                    } catch (e) {
                        throw new Error('拉取全部会话失败: ' + e.message);
                    } finally {
                        this.fetching = false;
                    }
                    setBtn('⇧ 上传中…', '#dc3545');
                }

                if (!state.targetResponse) {
                    // 拉取未成功：导出器已弹窗提示过具体原因（无凭证/无会话/全失败/异常），
                    // 不再重复弹误导性的"上传出错"
                    const lr = exporter.lastResult;
                    if (lr && lr.ok === false) {
                        logger.warn(`全部会话拉取未成功（${lr.reason}），已取消上传`);
                        if (auto) return lr.reason === '没有符合条件的会话' ? 'empty' : 'retry';
                        return;
                    }
                    throw new Error('全部会话拉取没有获得可上传的数据');
                }

                const data = JSON.parse(state.targetResponse);
                // 平台的标题轮询接口（doubao_title/kimi_title/qianwen_title/doubao_chat）
                // 会在消息数据之后到达并覆盖 platformType，这里归一为基础平台名
                let platform = data.batch_export ? data.platform : state.platformType;
                platform = String(platform || '').replace(/_(title|chat)$/, '');
                if (!platform) throw new Error('无法识别当前平台，请先打开一个对话');

                // 数据被标题响应覆盖时（豆包/豆包标题接口、Kimi标题接口），提示重新捕获
                const isTitleOnly = (data.downlink_body && data.downlink_body.get_conv_info_downlink_body &&
                    !data.downlink_body.pull_singe_chain_downlink_body) ||
                    (data.chat && !Array.isArray(data.messages));
                if (isTitleOnly) {
                    throw new Error('当前捕获到的只是会话标题信息（平台的标题接口在消息之后到达并覆盖了数据）。\n请刷新页面重新进入对话，待消息加载后再点上传。');
                }

                let okCount = 0, failCount = 0, msgTotal = 0, skipCount = 0;
                const failed = [];

                if (data.batch_export && (data.conversations || data.sessions)) {
                    // 批量导出的数据：逐会话上传
                    const items = data.conversations || data.sessions;
                    if (!items.length) throw new Error('拉取到的会话数为 0，没有可上传的数据');

                    // 增量检查：库中已是最新且无变化的会话直接跳过，不重传不重写
                    const idOf = (it) => String(it.chat_session_id || it.session_id || it.conversation_id || it.chat_id || '');
                    const updOf = (it) => it.updated_at ?? it.update_time ?? it.last_replied_at ?? it.created_at ?? it.create_time ?? '';
                    const cntOf = (it) => (it.messages || it.turns || it.convs || it.historyList || []).length;
                    let needSet = null; // null 表示检查失败，保守全量上传
                    try {
                        const chk = await this.httpPost('/check', {
                            platform: platform,
                            device_id: this.deviceId || undefined,
                            items: items.map(it => ({ conversation_id: idOf(it), updated: String(updOf(it)), count: cntOf(it) }))
                        });
                        if (chk.status === 200 && chk.json && chk.json.ok) {
                            needSet = new Set(chk.json.need_upload);
                            if (chk.json.skipped_count) logger.info(`增量检查: ${chk.json.skipped_count} 个会话库中已是最新，跳过`);
                        }
                    } catch (e) { /* 检查失败则全量上传 */ }

                    for (let i = 0; i < items.length; i++) {
                        if (this.stopRequested) break;
                        const itemId = idOf(items[i]);
                        if (needSet !== null && itemId && !needSet.has(itemId)) { skipCount++; continue; } // 库中已是最新
                        setBtn(`⇧ 上传中 ${i + 1}/${items.length}`, '#dc3545');
                        const r = await this.uploadOne(platform, items[i], { batch_type: 'all' });
                        if (r.ok) { okCount++; if (r.skipped) skipCount++; else msgTotal += r.count; }
                        else {
                            failCount++;
                            failed.push(`${items[i].title || items[i].name || items[i].session_id || ('#' + (i + 1))}: ${r.error}`);
                        }
                        await new Promise(res => setTimeout(res, 150)); // 限速
                    }
                    // 批次记录（失败不影响结果）
                    if (okCount - skipCount > 0) {
                        const batchNo = new Date().toISOString().replace(/\D/g, '').slice(0, 14) + '-' + Math.random().toString(36).slice(2, 6);
                        await this.httpPost('/batch-log', {
                            batch_no: batchNo,
                            platform: platform,
                            batch_type: 'all',
                            conversation_count: okCount - skipCount,
                            message_count: msgTotal,
                            failed_count: failCount,
                            client_version: SCRIPT_VERSION,
                            exported_at: new Date().toISOString()
                        }).catch(() => {});
                    }
                } else {
                    // 当前单个会话
                    const r = await this.uploadOne(platform, data, {
                        batch_type: 'single',
                        conversation_id: this.getConversationId(),
                        title: state.currentTitle
                    });
                    if (r.ok) { okCount = 1; if (r.skipped) skipCount = 1; else msgTotal = r.count; }
                    else { failCount = 1; failed.push(r.error); }
                }

                const stopNote = this.stopRequested ? '\n（已手动停止，剩余会话可再次点击继续上传）' : '';
                if (auto) {
                    // 自动模式：右下角 toast 轻提示，不弹窗打断用户
                    if (okCount) {
                        showToast(`☀ 每日自动上传完成：上传 ${okCount - skipCount} 个会话 / ${msgTotal} 条消息` +
                            (skipCount ? `，跳过 ${skipCount} 个（库中已是最新）` : '') +
                            (failCount ? `，失败 ${failCount} 个` : ''), 6000);
                        logger.info(`每日自动上传完成: 上传 ${okCount - skipCount} / 跳过 ${skipCount} / 失败 ${failCount}`);
                    } else if (failCount) {
                        showToast(`☀ 每日自动上传失败 ${failCount} 个会话，详情见控制台日志`, 6000);
                    }
                } else {
                    const scopeNote = data.batch_export ? '' : '\n（本次仅上传当前打开的会话；要上传全部会话，请在弹窗中选择"确定"）';
                    if (okCount) {
                        alert(`上传数据库完成${stopNote}${scopeNote}\n上传 ${okCount - skipCount} 个会话 / ${msgTotal} 条消息` +
                            (skipCount ? `\n跳过 ${skipCount} 个（库中已是最新）` : '') +
                            (failCount ? `\n失败 ${failCount} 个：\n${failed.slice(0, 5).join('\n')}` : ''));
                        logger.info(`数据库上传完成: 上传 ${okCount - skipCount} / 跳过 ${skipCount} / 失败 ${failCount}`);
                    } else {
                        alert(`上传失败${stopNote}：\n${failed.join('\n')}`);
                    }
                }
                // 全部失败(如上传服务临时不可用)→retry 下次重试；部分失败/成功→ok 不再重试
                if (auto) return (okCount === 0 && failCount > 0) ? 'retry' : 'ok';
            } catch (e) {
                if (auto) {
                    logger.error('每日自动上传出错', e);
                    return 'retry';
                }
                alert('上传出错: ' + e.message);
                logger.error('数据库上传出错', e);
            } finally {
                this.running = false;
                this.stopRequested = false;
                setBtn(originalText);
                // 自动模式：恢复暂存的捕获状态，清掉一次性筛选条件
                if (auto) {
                    if (this._autoSaved) {
                        state.targetResponse = this._autoSaved.targetResponse;
                        state.platformType = this._autoSaved.platformType;
                        state.currentTitle = this._autoSaved.currentTitle;
                        state.messageStats = this._autoSaved.messageStats;
                        state.convertedMd = this._autoSaved.convertedMd;
                        this._autoSaved = null;
                        try { ui.updateButtonStatus(); } catch (e) {}
                    }
                    this._autoFilter = null;
                }
            }
        },

        // 每日自动上传：页面加载后延迟触发一次，把"昨天（本地时区自然日）有更新"的会话静默上传
        // 每个平台每天只完成一次（localStorage 按天去重，仅在任务有终态结果后写入标记）；
        // 凭证未就绪/临时失败不写标记，下次页面加载自动重试
        autoDaily: async function() {
            try {
                const h = location.hostname;
                const platform = h.includes('doubao') ? 'doubao'
                    : h.includes('qianwen') ? 'qianwen'
                    : h.includes('deepseek') ? 'deepseek'
                    : h.includes('yuanbao') ? 'yuanbao'
                    : h.includes('kimi') ? 'kimi'
                    : h.includes('xfyun') ? 'iflytek'
                    : null;
                if (!platform) return; // 无批量导出器的平台不参与
                const key = 'aiExportAutoDaily_' + platform;
                const today = new Date().toLocaleDateString('sv'); // YYYY-MM-DD（本地时区）
                let last = '';
                try { last = localStorage.getItem(key) || ''; } catch (e) {}
                if (last === today) {
                    // 静默跳过但留日志：便于排查"为什么今天不再自动上传"
                    logger.info(`每日自动上传：${platform} 今天已完成过（标记=${last}），本次跳过`);
                    return;
                }

                // 昨天自然日 = [昨天0点, 今天0点)（本地时区）
                const now = new Date();
                const yEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
                const yStart = yEnd - 86400000;
                // 各平台列表项的更新时间字段名不一致，逐一尝试
                const tOf = (it) => it.updated_at ?? it.update_time ?? it.last_replied_at ?? it.lastRepliedAt
                    ?? it.updatedAt ?? it.updateTime ?? it.created_at ?? it.create_time ?? it.createTime ?? 0;
                // 时间转毫秒：纯数字(含字符串形式,如豆包的 "1762889203")按秒/毫秒时间戳处理，其余按日期串解析
                const toMs = (t) => {
                    if (typeof t === 'number' && isFinite(t)) return t > 1e12 ? t : t * 1000;
                    if (typeof t === 'string') {
                        const s = t.trim();
                        if (/^\d+$/.test(s)) { const n = Number(s); return n > 1e12 ? n : n * 1000; }
                        const p = Date.parse(s); return isNaN(p) ? 0 : p;
                    }
                    return 0;
                };
                this._autoFilter = (it) => {
                    const ms = toMs(tOf(it));
                    return ms >= yStart && ms < yEnd;
                };
                const ymd = new Date(yStart).toLocaleDateString();
                logger.info(`每日自动上传：检查 ${ymd} 有更新的会话...`);
                showToast(`☀ 每日自动上传：正在检查 ${ymd} 的会话...`, 4000);
                const r = await this.start(null, true);
                if (r === 'retry') {
                    logger.info('每日自动上传：环境未就绪或临时失败，不写标记，下次页面加载时重试');
                    showToast('☀ 每日自动上传未完成（凭证未就绪或临时失败），下次打开页面自动重试', 6000);
                    return;
                }
                // 有终态（已上传/无需上传）才写当天标记
                try { localStorage.setItem(key, today); } catch (e) {}
                if (r === 'empty') {
                    logger.info(`每日自动上传：${ymd} 没有需要上传的会话`);
                    showToast(`☀ 每日自动上传：${ymd} 没有需要上传的会话`, 5000);
                }
            } catch (e) {
                logger.error('每日自动上传失败', e);
            }
        }
    };

    autoSaver.loadSettings(); // 启动时预取面板设置，供手动下载时确定保存目录

    // 每日自动上传：页面加载 15 秒后触发一次（等待登录凭证捕获、避开页面初始化高峰），
    // 每平台每天只跑一次；正在手动操作时自动让位（autoDaily/start 内部有守卫）
    setTimeout(() => {
        try { uploader.autoDaily(); } catch (e) {}
    }, 15000);

    // 调试钩子：控制台执行 __aiExport.triggerAutoDaily() 可立即手动触发每日自动上传（不受15秒等待限制，
    // 但仍受"每天一次"标记约束——测试时可先 localStorage.removeItem('aiExportAutoDaily_<平台>')）
    try {
        const _dbgWin = (typeof unsafeWindow !== 'undefined') ? unsafeWindow : window;
        _dbgWin.__aiExport = {
            version: SCRIPT_VERSION,
            triggerAutoDaily: () => uploader.autoDaily()
        };
        logger.info(`调试钩子就绪：控制台可用 __aiExport.triggerAutoDaily() 手动触发每日自动上传（v${SCRIPT_VERSION}）`);
    } catch (e) {}

    const exportHandlers = {
        json: () => {
            if (!state.targetResponse) {
                alert('还没有发现有效的对话记录。\n请等待目标响应或进行一些对话。');
                return false;
            }

            try {
                const fileName = getExportFilename('json');
                return utils.createBlobDownload(
                    state.targetResponse,
                    'application/json',
                    fileName
                );
            } catch (e) {
                logger.error('JSON导出失败', e);
                alert('导出过程中发生错误，请查看控制台了解详情。');
                return false;
            }
        },

        markdown: () => {
            if (!state.convertedMd) {
                alert('还没有发现有效的对话记录。\n请等待目标响应或进行一些对话。');
                return false;
            }

            try {
                const fileName = getExportFilename('md');
                return utils.createBlobDownload(
                    state.convertedMd,
                    'text/markdown',
                    fileName
                );
            } catch (e) {
                logger.error('Markdown导出失败', e);
                alert(`导出失败: ${e.message}`);
                return false;
            }
        },

        word: () => {
            if (!state.convertedMd) {
                alert('还没有发现有效的对话记录。\n请等待目标响应或进行一些对话。');
                return;
            }

            try {
                const fileName = getExportFilename('docx');
                const chatName = fileName.replace(/_[\d_-]+\.docx$/, ''); // 移除时间戳部分作为标题

                const wordButton = document.getElementById('downloadWordButton');
                const originalText = wordButton.innerHTML;
                wordButton.innerHTML = '<span class="gm-spinner"></span>生成中...';
                wordButton.disabled = true;

                GM_xmlhttpRequest({
                    method: "POST",
                    url: CONFIG.API_ENDPOINT_WORD,
                    headers: { "Content-Type": "application/json" },
                    data: JSON.stringify({
                        markdown: state.convertedMd,
                        title: chatName
                    }),
                    responseType: 'blob',
                    onload: function(response) {
                        wordButton.innerHTML = originalText;
                        wordButton.disabled = false;

                        if (response.status >= 200 && response.status < 300) {
                            try {
                                utils.createBlobDownload(response.response,
                                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                    fileName);
                                logger.info(`成功下载Word文件: ${fileName}`);
                            } catch (e) {
                                alert('下载文件时出错: ' + e.message);
                                logger.error("处理Word下载时出错", e);
                            }
                        } else {
                            const reader = new FileReader();
                            reader.onload = function() {
                                try {
                                    const errorResult = JSON.parse(this.result);
                                    alert(`导出失败: ${errorResult.error || '未知错误'}`);
                                } catch (e) {
                                    alert(`导出失败，状态码: ${response.status}。无法解析错误信息。`);
                                }
                            };
                            reader.readAsText(response.response);
                        }
                    },
                    onerror: function(response) {
                        wordButton.innerHTML = originalText;
                        wordButton.disabled = false;
                        alert(`请求错误: ${response.statusText || '无法连接到服务器'}`);
                    }
                });
            } catch (e) {
                logger.error('Word导出初始化失败', e);
                alert('导出初始化失败: ' + e.message);
            }
        }
    };

    // ============ 自动滚动加载全部历史记录 ============
    // 反复把聊天区域滚到顶部，触发平台去请求更早的轮次；
    // 通过“滚动高度”和“累积池消息数”是否还在增长来判断是否已加载完。
    const autoScroller = {
        running: false,
        stopRequested: false,

        sleep: (ms) => new Promise(resolve => setTimeout(resolve, ms)),

        // 寻找聊天消息所在的滚动容器：可滚动、在视口内、面积够大、可滚动距离最长的元素
        findScrollContainer: () => {
            let best = null;
            let bestScore = 0;
            const elements = document.querySelectorAll('div, main, section, ul, ol');
            for (const el of elements) {
                const scrollable = el.scrollHeight - el.clientHeight;
                if (scrollable < 100) continue;
                const style = getComputedStyle(el);
                if (!/(auto|scroll|overlay)/.test(style.overflowY)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 250 || rect.height < 150) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
                const score = scrollable * Math.sqrt(rect.width * rect.height);
                if (score > bestScore) {
                    bestScore = score;
                    best = el;
                }
            }
            // 有些页面是整个窗口在滚动，作为兜底
            const root = document.scrollingElement;
            if (!best && root && root.scrollHeight - root.clientHeight > 100) {
                best = root;
            }
            return best;
        },

        start: async function(button) {
            // 运行中再点一次 = 请求停止
            if (this.running) {
                this.stopRequested = true;
                button.innerText = '⏹ 正在停止...';
                return;
            }

            let container = this.findScrollContainer();
            if (!container) {
                alert('没有找到可滚动的聊天区域。\n请确认页面里已经打开了一个对话，且对话内容超过一屏。');
                return;
            }

            this.running = true;
            this.stopRequested = false;
            const originalText = '⏫ 加载全部';
            button.style.backgroundColor = '#dc3545';
            logger.info('开始自动滚动加载历史记录...');

            let idleRounds = 0; // 连续无变化的轮数
            let rounds = 0;
            let lastHeight = container.scrollHeight;
            let lastCount = state.mergeStore.map.size;

            while (!this.stopRequested && idleRounds < 4 && rounds < 300) {
                // 页面重新渲染可能导致容器失效，重新查找
                if (!container.isConnected) {
                    container = this.findScrollContainer();
                    if (!container) break;
                }

                container.scrollTop = 0;
                // 有些虚拟列表靠wheel事件触发加载，补发一个向上滚动事件
                container.dispatchEvent(new WheelEvent('wheel', { deltaY: -600, bubbles: true, cancelable: true }));

                await this.sleep(800); // 等待平台请求并渲染更早的消息
                rounds++;

                const height = container.scrollHeight;
                const count = state.mergeStore.map.size;
                if (height > lastHeight + 5 || count > lastCount) {
                    idleRounds = 0;
                } else {
                    idleRounds++;
                }
                lastHeight = height;
                lastCount = count;

                button.innerText = `⏳ 已捕获 ${count} 条（点击停止）`;
            }

            this.running = false;
            const finalCount = state.mergeStore.map.size;
            logger.info(`自动滚动结束：共捕获 ${finalCount} 条消息，滚动 ${rounds} 轮`);

            button.innerText = this.stopRequested ? `⏹ 已停止，共 ${finalCount} 条` : `✅ 完成，共 ${finalCount} 条`;
            button.style.backgroundColor = '#28a745';
            if (!this.stopRequested) {
                // 扩展面板开启自动保存时，加载完成后无需再点格式按钮，直接自动保存
                autoSaver.autoSaveIfNeeded('加载全部完成');
            }
            setTimeout(() => {
                button.innerText = originalText;
                button.style.backgroundColor = '#007bff';
            }, 4000);
        }
    };
    // ============ 自动滚动模块结束 ============

    // ============ 豆包"导出全部最近会话"模块 ============
    // 通过直接重放豆包IM接口拉取左侧"最近"列表中的全部会话：
    // 1. /im/chain/recent_conv (cmd 3200) 分页拉取会话列表
    // 2. /im/chain/single (cmd 3100) 按anchor_index分页拉取每个会话的完整历史
    // 关键点：Content-Type 必须是非标准的 "application/json; encoding=utf-8"（encoding= 而非 charset=），
    // 且URL必须携带完整query串（裸路径会返回空响应）
    const DOUBAO_FALLBACK_QUERY = '?version_code=20800&language=zh&device_platform=web&doubao_device_platform=web&aid=497858&real_aid=497858&pkg_type=release_version&device_id=7697538715166950954&pc_version=3.41.4&doubao_pc_version=3.41.4&web_id=7697538742428927526&tea_uuid=7697538742428927526&region=CN&sys_region=CN&samantha_web=1&web_platform=browser&use-olympus-account=1&web_tab_id=b70804d1-e2d8-4496-a777-e7e4f8942548';

    function doubaoUuid() {
        if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    // 批量拉取的偶发错误（504 网关超时、712010702 等）：退避重试最多3次，
    // 重试仍失败才抛给上层（上层按单会话失败跳过并在结束时汇总）
    async function withBatchRetry(fn, label) {
        let lastErr = null;
        for (let attempt = 0; attempt <= 2; attempt++) {
            if (attempt > 0) {
                const delay = 1500 * attempt;
                logger.warn(`${label}请求失败（${lastErr.message}），${delay}ms 后第 ${attempt + 1} 次尝试`);
                await new Promise(r => setTimeout(r, delay));
            }
            try {
                return await fn();
            } catch (e) {
                lastErr = e;
            }
        }
        throw lastErr;
    }

    async function doubaoApiPost(path, bodyObj) {
        const query = state.doubaoApiQuery || DOUBAO_FALLBACK_QUERY;
        return withBatchRetry(async () => {
            const resp = await fetch(`https://www.doubao.com${path}${query}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json; encoding=utf-8',
                    'Agw-Js-Conv': 'str',
                    'Accept': 'application/json, text/plain, */*'
                },
                body: JSON.stringify(bodyObj),
                credentials: 'include'
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const sc = data?.status_code;
            if (sc !== undefined && sc !== 0) {
                throw new Error(`status_code ${sc}: ${data?.status_msg || '未知错误'}`);
            }
            return data;
        }, '豆包接口');
    }

    // 分页拉取"最近"会话列表：首页 direction=3/conv_version=0，
    // 之后 direction=1/conv_version=next_conv_version，直到 has_more=false。
    // 注意：响应里 next_conv_version 是字符串，翻页请求必须转成数字（字符串会报 712010702）
    async function fetchDoubaoRecentConvList(onProgress) {
        const conversations = [];
        const seen = new Set();
        let convVersion = 0;
        let direction = 3;
        let hasMore = true;
        let pages = 0;

        while (hasMore && pages < 50) {
            const isFirstPage = pages === 0;
            const pullBody = {
                limit: isFirstPage ? 20 : 50,
                filter: { conversation_type: [], project_filter: 1 },
                message_count_per_conv: 0,
                api_version: 1,
                conv_version: convVersion,
                direction: direction,
                option: {
                    not_need_message: true,
                    need_complete_conversation: true,
                    need_coco_bot: isFirstPage,
                    need_pc_pin_chain: true,
                    pc_pin_query_type: 1,
                    exclude_archive: true,
                    only_archive: false
                }
            };
            const data = await doubaoApiPost('/im/chain/recent_conv', {
                cmd: 3200,
                uplink_body: { pull_recent_conv_chain_uplink_body: pullBody },
                sequence_id: doubaoUuid(),
                channel: 2,
                version: '1'
            });

            const dl = data?.downlink_body?.pull_recent_conv_chain_downlink_body;
            for (const cell of (dl?.cells || [])) {
                const conv = cell?.conversation;
                if (conv?.conversation_id && !seen.has(String(conv.conversation_id))) {
                    seen.add(String(conv.conversation_id));
                    conversations.push(conv);
                }
            }
            hasMore = !!dl?.has_more;
            pages++;
            onProgress?.(conversations.length, pages);
            if (hasMore) {
                const next = dl?.next_conv_version;
                if (next === undefined || next === null || String(next) === String(convVersion)) break; // 无进展保护
                convVersion = Number(next); // 字符串转数字，否则服务端报 712010702
                direction = 1;
                await new Promise(r => setTimeout(r, 250)); // 限速
            }
        }
        return conversations;
    }

    // 拉取单个会话的完整历史：anchor_index 从 int64 最大值开始向前翻页，
    // 锚点消息会在相邻两页重复出现，按 message_id 去重
    async function fetchDoubaoConversationMessages(conv) {
        const msgMap = new Map();
        let anchor = 9007199254740991;
        let hasMore = true;
        let pages = 0;

        while (hasMore && pages < 500) {
            const data = await doubaoApiPost('/im/chain/single', {
                cmd: 3100,
                uplink_body: {
                    pull_singe_chain_uplink_body: {
                        conversation_id: String(conv.conversation_id),
                        anchor_index: anchor,
                        conversation_type: conv.conversation_type ?? 3,
                        direction: 1,
                        limit: 20,
                        ext: {},
                        filter: { index_list: [] },
                        evaluate_ab_params: '',
                        evaluate_common_params: '',
                        option: { lazy_load_strategy: 0 }
                    }
                },
                sequence_id: doubaoUuid(),
                channel: 2,
                version: '1'
            });

            const dl = data?.downlink_body?.pull_singe_chain_downlink_body;
            const messages = dl?.messages || [];
            for (const m of messages) {
                const id = m.message_id ?? m.server_message_id ?? m.local_message_id;
                if (id !== undefined && id !== null) msgMap.set(String(id), m);
            }
            hasMore = !!dl?.has_more;
            pages++;

            if (hasMore && messages.length) {
                let minIic = null;
                for (const m of messages) {
                    const iic = Number(m.index_in_conv);
                    if (!isNaN(iic) && (minIic === null || iic < minIic)) minIic = iic;
                }
                if (minIic === null || minIic >= anchor) break; // 停滞保护
                anchor = minIic;
                await new Promise(r => setTimeout(r, 150)); // 限速
            } else {
                break;
            }
        }
        return [...msgMap.values()].sort((a, b) => (a.create_time ?? 0) - (b.create_time ?? 0));
    }

    const doubaoBatchExporter = {
        running: false,
        stopRequested: false,
        lastResult: null, // 最近一次 start 的结束状态 {ok, count} / {ok:false, reason}
        sleep: (ms) => new Promise(resolve => setTimeout(resolve, ms)),

        // 统计字数（与单会话导出的统计口径一致）
        countChars: (messages) => (messages || []).reduce((acc, msg) => {
            try {
                const contentObj = JSON.parse(msg.content || '{}');
                return acc + (contentObj.text?.length || 0);
            } catch {
                return acc + (msg.content?.length || 0);
            }
        }, 0),

        start: async function(button, opts) {
            // opts.filter: 自动模式筛选函数(只导出符合条件的会话)；opts.silent: 静默(自动上传用,不弹窗不自动保存)
            // 运行中再点一次 = 请求停止（自动任务不请求停止，直接让位）
            if (this.running) {
                if (opts && opts.silent) return; // 用户正在手动导出，自动任务让位
                this.stopRequested = true;
                button.innerText = '⏹ 正在停止...';
                return;
            }

            const originalText = '📚 全部会话';
            this.running = true;
            this.stopRequested = false;
            state.doubaoBatchBusy = true;
            button.style.backgroundColor = '#dc3545';
            logger.info('开始豆包全部会话批量导出...');

            try {
                // 第一步：拉取"最近"会话列表
                const conversations = await fetchDoubaoRecentConvList((count) => {
                    button.innerText = `⏳ 会话列表 ${count}...`;
                });
                if (!conversations.length) {
                    this.lastResult = { ok: false, reason: '没有获取到任何会话' };
                    if (!(opts && opts.silent)) alert('没有获取到任何会话。\n请确认已登录豆包且左侧"最近"列表非空，然后刷新页面重试。');
                    return;
                }
                logger.info(`豆包批量导出：共 ${conversations.length} 个会话`);

                // 自动模式(opts.filter)：只导出符合条件的会话；否则超过50个让用户选择范围
                let toExport = conversations;
                if (opts && typeof opts.filter === 'function') {
                    toExport = conversations.filter(opts.filter);
                    if (!toExport.length) {
                        this.lastResult = { ok: false, reason: '没有符合条件的会话' };
                        if (!opts.silent) alert('没有符合条件的会话（如：所选日期没有新的对话）。');
                        return;
                    }
                    logger.info(`自动模式：筛选出 ${toExport.length} 个符合条件的会话`);
                } else if (conversations.length > 50) {
                    const countText = conversations.length >= 1000
                        ? `${conversations.length}+ 个会话（已达列表拉取上限，仅含最近的1000个）`
                        : `${conversations.length} 个会话`;
                    const ok = confirm(`共找到 ${countText}。\n\n` +
                        `全部导出需要逐个拉取历史，可能耗时很久（可随时再次点击按钮停止）。\n\n` +
                        `【确定】导出全部\n【取消】只导出最近的 50 个`);
                    if (!ok) toExport = conversations.slice(0, 50);
                }

                // 第二步：逐个拉取完整历史并转换为Markdown
                const allMd = [];
                const allJson = [];
                const failed = [];
                let totalMessages = 0;
                let totalQuestions = 0;
                let totalChars = 0;

                for (let i = 0; i < toExport.length; i++) {
                    if (this.stopRequested) break;
                    const conv = toExport[i];
                    const title = conv.name || `未命名会话_${conv.conversation_id}`;
                    button.innerText = `⏳ ${i + 1}/${toExport.length} ${title}`.slice(0, 22);

                    try {
                        const messages = await fetchDoubaoConversationMessages(conv);
                        if (messages.length) {
                            totalMessages += messages.length;
                            totalQuestions += messages.filter(m => m.user_type === 1).length;
                            totalChars += this.countChars(messages);

                            // 构造与拦截响应一致的结构，复用现有豆包转换器
                            const convData = {
                                conversation_id: String(conv.conversation_id),
                                name: title,
                                downlink_body: { pull_singe_chain_downlink_body: { messages } }
                            };
                            allMd.push(platformConverters.doubao(convData, title));
                            allJson.push({
                                conversation_id: String(conv.conversation_id),
                                name: title,
                                create_time: conv.create_time,
                                update_time: conv.update_time,
                                message_count: messages.length,
                                messages
                            });
                        }
                    } catch (e) {
                        logger.error(`拉取会话失败: ${title}`, e);
                        failed.push({ title, error: e.message });
                    }
                    await this.sleep(250);
                }

                if (!allJson.length) {
                    this.lastResult = { ok: false, reason: '所有会话拉取失败' };
                    if (!(opts && opts.silent)) alert('没有成功导出任何会话。' +
                        (failed.length ? `\n失败示例: ${failed[0].title} - ${failed[0].error}` : ''));
                    return;
                }

                // 第三步：填充导出状态，复用现有的 JSON/MD/Word 按钮
                state.platformType = 'doubao';
                state.lastUpdateTime = new Date().toLocaleTimeString();
                state.currentTitle = `豆包全部${allJson.length}个会话`;
                state.messageStats = {
                    totalTokens: 0,
                    totalChars: totalChars,
                    fileCount: 0,
                    questions: totalQuestions,
                    convTurns: totalMessages
                };
                state.targetResponse = JSON.stringify({
                    batch_export: true,
                    platform: 'doubao',
                    conversation_count: allJson.length,
                    conversations: allJson
                });
                state.convertedMd = `# 豆包全部会话导出（共 ${allJson.length} 个会话）\n\n` + allMd.join('\n\n');
                ui.updateButtonStatus();
                this.lastResult = { ok: true, count: allJson.length };

                const stopNote = this.stopRequested ? '（已提前停止）' : '';
                const failNote = failed.length
                    ? `\n失败 ${failed.length} 个: ${failed.map(f => f.title).slice(0, 5).join('、')}${failed.length > 5 ? ' 等' : ''}`
                    : '';
                if (!(opts && opts.silent)) {
                    autoSaver.autoSaveIfNeeded('豆包批量导出');
                    alert(`豆包批量导出完成${stopNote}：\n成功 ${allJson.length} 个会话 / ${totalMessages} 条消息` +
                        failNote + '\n\n扩展面板开启自动保存后，文件会自动存入设定的目录；\n也可点击 JSON / MD / Word 按钮手动保存。');
                }
                logger.info(`豆包批量导出完成: ${allJson.length} 会话 / ${totalMessages} 消息 / ${failed.length} 失败`);
            } catch (e) {
                logger.error('豆包批量导出失败', e);
                this.lastResult = { ok: false, reason: e.message };
                if (!(opts && opts.silent)) alert(`豆包批量导出失败: ${e.message}\n请刷新页面后重试。`);
            } finally {
                this.running = false;
                this.stopRequested = false;
                state.doubaoBatchBusy = false;
                button.innerText = originalText;
                button.style.backgroundColor = '#007bff';
            }
        }
    };
    // ============ 豆包批量导出模块结束 ============

    // ============ 千问"导出全部最近会话"模块 ============
    // 通过直接重放千问接口拉取左侧"最近"列表中的全部会话：
    // 1. POST /api/v2/session/changes 枚举会话（last_sync_time 游标翻页）
    // 2. GET /api/v1/session/msg/list?session_id=... 拉取每个会话的完整轮次（page_size 一次拉全）
    // 鉴权：X-DeviceId / X-XSRF-TOKEN / x-user-id 三个头 + Cookie，均从拦截到的真实请求动态捕获；
    // 接口的 clt-acs-* 签名头经实测可省略、且不绑定 session_id
    function captureQianwenAuthHeader(header, value) {
        if (!value) return;
        const h = String(header).toLowerCase();
        if (h === 'x-deviceid') state.qianwenAuth.deviceId = value;
        else if (h === 'x-xsrf-token') state.qianwenAuth.xsrf = value;
        else if (h === 'x-user-id') state.qianwenAuth.userId = value;
    }

    const qianwenBatchExporter = {
        running: false,
        stopRequested: false,
        lastResult: null, // 最近一次 start 的结束状态 {ok, count} / {ok:false, reason}
        sleep: (ms) => new Promise(resolve => setTimeout(resolve, ms)),

        hasAuth: () => !!(state.qianwenAuth.deviceId && state.qianwenAuth.xsrf && state.qianwenAuth.userId),

        authHeaders: () => ({
            'Content-Type': 'application/json',
            'X-Platform': 'pc_tongyi',
            'X-DeviceId': state.qianwenAuth.deviceId,
            'X-XSRF-TOKEN': state.qianwenAuth.xsrf,
            'x-user-id': state.qianwenAuth.userId
        }),

        // 公共query参数（优先复用捕获到的真实query串，否则用已验证的静态模板构造）
        listUrl: () => {
            const base = 'https://chat2-api.qianwen.com/api/v2/session/changes';
            if (state.qianwenListQuery) return base + state.qianwenListQuery;
            const p = new URLSearchParams({
                biz_id: 'ai_qwen', chat_client: 'h5', device: 'pc', fr: 'pc', pr: 'qwen',
                ut: state.qianwenAuth.deviceId, la: 'zh-CN', tz: 'Asia/Shanghai',
                wv: '4.10.7', tq: 'SD', ve: '4.10.7'
            });
            return base + '?' + p.toString();
        },

        msgUrl: (sessionId, pageSize, forward) => {
            const base = 'https://chat2-api.qianwen.com/api/v1/session/msg/list';
            let p;
            if (state.qianwenMsgQuery) {
                p = new URLSearchParams(state.qianwenMsgQuery.slice(1));
            } else {
                p = new URLSearchParams({
                    biz_id: 'ai_qwen', chat_client: 'h5', device: 'pc', fr: 'pc', pr: 'qwen',
                    ut: state.qianwenAuth.deviceId, la: 'zh-CN', tz: 'Asia/Shanghai',
                    wv: '4.10.7', tq: 'SD', ve: '4.10.7',
                    nonce: doubaoUuid(), timestamp: String(Date.now())
                });
            }
            p.set('session_id', String(sessionId));
            p.set('page_size', String(pageSize));
            p.set('forward', forward ? 'true' : 'false');
            p.set('include_pos', 'false');
            p.set('return_response_messages', 'true');
            p.set('event_filter', 'all');
            return base + '?' + p.toString();
        },

        // 枚举全部会话：last_sync_time=0 起步，响应按 updated_at 升序，
        // 用本页最后一项的 updated_at 作为下次游标，直到 has_more=false
        fetchQianwenSessions: async function(onProgress) {
            const sessions = [];
            const seen = new Set();
            let lastSyncTime = 0;
            let hasMore = true;
            let pages = 0;

            while (hasMore && pages < 50) {
                const resp = await withBatchRetry(() => fetch(this.listUrl(), {
                    method: 'POST',
                    headers: this.authHeaders(),
                    body: JSON.stringify({ last_sync_time: lastSyncTime, limit: 100 }),
                    credentials: 'include'
                }), '千问会话列表');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const j = await resp.json();
                if (j.code !== 0) throw new Error(`code ${j.code}: ${j.msg || '接口错误'}`);

                const list = j.data?.list || [];
                for (const s of list) {
                    if (s.session_id && !seen.has(String(s.session_id))) {
                        seen.add(String(s.session_id));
                        sessions.push(s);
                    }
                }
                hasMore = !!j.data?.has_more;
                pages++;
                onProgress?.(sessions.length, pages);
                if (hasMore && list.length) {
                    const next = Number(list[list.length - 1].updated_at);
                    if (isNaN(next) || next <= lastSyncTime) break; // 停滞保护
                    lastSyncTime = next;
                    await this.sleep(250); // 限速
                } else if (hasMore) {
                    break; // 无数据却还有 more，防御退出
                }
            }
            return sessions;
        },

        // 拉取单个会话的完整轮次：page 参数被服务端忽略，page_size 一次拉全；
        // 万一超长（have_next_page 仍为 true），再从最旧端拉一段按 req_id 去重合并
        fetchQianwenMessages: async function(sessionId) {
            const get = async (forward) => {
                const resp = await withBatchRetry(() => fetch(this.msgUrl(sessionId, 10000, forward), {
                    method: 'GET',
                    headers: this.authHeaders(),
                    credentials: 'include'
                }), '千问消息');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const j = await resp.json();
                if (j.code !== 0) throw new Error(`code ${j.code}: ${j.msg || '接口错误'}`);
                return j.data || {};
            };
            const newest = await get(false);
            let turns = newest.list || [];
            if (newest.have_next_page) {
                const oldest = await get(true);
                const key = (t) => String(t.req_id ?? t.pos ?? `${t.request_timestamp}|${t.created_at}`);
                const map = new Map();
                for (const t of [...turns, ...(oldest.list || [])]) map.set(key(t), t);
                turns = [...map.values()];
            }
            return turns;
        },

        // 统计字数（与单会话导出的统计口径一致）
        countChars: (turns) => (turns || []).reduce((acc, turn) => {
            const q = (turn.request_messages || []).reduce(
                (s, m) => s + (m.mime_type === 'text/plain' ? String(m.content || '').length : 0), 0);
            const a = (turn.response_messages || []).reduce(
                (s, m) => s + (m.mime_type === 'multi_load/iframe' && m.content ? String(m.content).length : 0), 0);
            return acc + q + a;
        }, 0),

        start: async function(button, opts) {
            // opts.filter: 自动模式筛选函数(只导出符合条件的会话)；opts.silent: 静默(自动上传用,不弹窗不自动保存)
            // 运行中再点一次 = 请求停止（自动任务不请求停止，直接让位）
            if (this.running) {
                if (opts && opts.silent) return; // 用户正在手动导出，自动任务让位
                this.stopRequested = true;
                button.innerText = '⏹ 正在停止...';
                return;
            }
            if (!this.hasAuth()) {
                this.lastResult = { ok: false, reason: '没有登录凭证' };
                if (!(opts && opts.silent)) alert('还没有捕获到千问的登录凭证。\n\n请先刷新页面并打开一个对话，' +
                    '等左侧"最近"列表加载出来后再点此按钮。');
                return;
            }

            const originalText = '📚 全部会话';
            this.running = true;
            this.stopRequested = false;
            state.qianwenBatchBusy = true;
            button.style.backgroundColor = '#dc3545';
            logger.info('开始千问全部会话批量导出...');

            try {
                // 第一步：拉取全部会话列表，按更新时间从新到旧排序
                const sessions = await this.fetchQianwenSessions((count) => {
                    button.innerText = `⏳ 会话列表 ${count}...`;
                });
                if (!sessions.length) {
                    this.lastResult = { ok: false, reason: '没有获取到任何会话' };
                    if (!(opts && opts.silent)) alert('没有获取到任何会话。\n请确认已登录千问且左侧"最近"列表非空，然后刷新页面重试。');
                    return;
                }
                sessions.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
                logger.info(`千问批量导出：共 ${sessions.length} 个会话`);

                // 自动模式(opts.filter)：只导出符合条件的会话；否则超过50个让用户选择范围
                let toExport = sessions;
                if (opts && typeof opts.filter === 'function') {
                    toExport = sessions.filter(opts.filter);
                    if (!toExport.length) {
                        this.lastResult = { ok: false, reason: '没有符合条件的会话' };
                        if (!opts.silent) alert('没有符合条件的会话（如：所选日期没有新的对话）。');
                        return;
                    }
                    logger.info(`自动模式：筛选出 ${toExport.length} 个符合条件的会话`);
                } else if (sessions.length > 50) {
                    const countText = sessions.length >= 5000
                        ? `${sessions.length}+ 个会话（已达列表拉取上限）`
                        : `${sessions.length} 个会话`;
                    const ok = confirm(`共找到 ${countText}。\n\n` +
                        `全部导出需要逐个拉取历史，可能耗时很久（可随时再次点击按钮停止）。\n\n` +
                        `【确定】导出全部\n【取消】只导出最近的 50 个`);
                    if (!ok) toExport = sessions.slice(0, 50);
                }

                // 第二步：逐个拉取完整历史并转换为Markdown
                const allMd = [];
                const allJson = [];
                const failed = [];
                let totalTurns = 0;
                let totalChars = 0;

                for (let i = 0; i < toExport.length; i++) {
                    if (this.stopRequested) break;
                    const s = toExport[i];
                    const title = s.title || `未命名会话_${s.session_id}`;
                    button.innerText = `⏳ ${i + 1}/${toExport.length} ${title}`.slice(0, 22);

                    try {
                        const turns = await this.fetchQianwenMessages(s.session_id);
                        if (turns.length) {
                            totalTurns += turns.length;
                            totalChars += this.countChars(turns);

                            // 构造与拦截响应一致的结构，复用现有千问转换器
                            const convData = { data: { list: turns } };
                            allMd.push(platformConverters.qianwen(convData, title));
                            allJson.push({
                                session_id: String(s.session_id),
                                title: title,
                                created_at: s.created_at,
                                updated_at: s.updated_at,
                                turn_count: turns.length,
                                turns
                            });
                        }
                    } catch (e) {
                        logger.error(`拉取会话失败: ${title}`, e);
                        failed.push({ title, error: e.message });
                    }
                    await this.sleep(250);
                }

                if (!allJson.length) {
                    this.lastResult = { ok: false, reason: '所有会话拉取失败' };
                    if (!(opts && opts.silent)) alert('没有成功导出任何会话。' +
                        (failed.length ? `\n失败示例: ${failed[0].title} - ${failed[0].error}` : ''));
                    return;
                }

                // 第三步：填充导出状态，复用现有的 JSON/MD/Word 按钮
                state.platformType = 'qianwen';
                state.lastUpdateTime = new Date().toLocaleTimeString();
                state.currentTitle = `千问全部${allJson.length}个会话`;
                state.messageStats = {
                    totalTokens: 0,
                    totalChars: totalChars,
                    fileCount: 0,
                    questions: 0,
                    convTurns: totalTurns
                };
                state.targetResponse = JSON.stringify({
                    batch_export: true,
                    platform: 'qianwen',
                    session_count: allJson.length,
                    sessions: allJson
                });
                state.convertedMd = `# 千问全部会话导出（共 ${allJson.length} 个会话）\n\n` + allMd.join('\n\n');
                ui.updateButtonStatus();
                this.lastResult = { ok: true, count: allJson.length };

                const stopNote = this.stopRequested ? '（已提前停止）' : '';
                const failNote = failed.length
                    ? `\n失败 ${failed.length} 个: ${failed.map(f => f.title).slice(0, 5).join('、')}${failed.length > 5 ? ' 等' : ''}`
                    : '';
                if (!(opts && opts.silent)) {
                    autoSaver.autoSaveIfNeeded('千问批量导出');
                    alert(`千问批量导出完成${stopNote}：\n成功 ${allJson.length} 个会话 / ${totalTurns} 轮对话` +
                        failNote + '\n\n扩展面板开启自动保存后，文件会自动存入设定的目录；\n也可点击 JSON / MD / Word 按钮手动保存。');
                }
                logger.info(`千问批量导出完成: ${allJson.length} 会话 / ${totalTurns} 轮 / ${failed.length} 失败`);
            } catch (e) {
                logger.error('千问批量导出失败', e);
                this.lastResult = { ok: false, reason: e.message };
                if (!(opts && opts.silent)) alert(`千问批量导出失败: ${e.message}\n请刷新页面后重试。`);
            } finally {
                this.running = false;
                this.stopRequested = false;
                state.qianwenBatchBusy = false;
                button.innerText = originalText;
                button.style.backgroundColor = '#007bff';
            }
        }
    };
    // ============ 千问批量导出模块结束 ============

    // ============ 通用批量导出运行器 ============
    // DeepSeek/元宝/Kimi/星火共用：各平台的列表/消息拉取与数据结构通过 spec 注入，
    // 「范围选择→逐会话拉取→转换→汇总→自动保存→结果提示」流程与豆包/千问一致
    const genericBatchExporter = (spec) => {
        // 注入sleep等公共方法，供spec内部的fetchList/fetchMessages等使用
        spec.sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
        return {
        running: false,
        stopRequested: false,
        lastResult: null, // 最近一次 start 的结束状态 {ok, count} / {ok:false, reason}
        sleep: spec.sleep,

        start: async function(button, opts) {
            // 运行中再点一次 = 请求停止（自动任务不请求停止，直接让位）
            if (this.running) {
                if (opts && opts.silent) return; // 用户正在手动导出，自动任务让位
                this.stopRequested = true;
                button.innerText = '⏹ 正在停止...';
                return;
            }
            if (spec.hasAuth && !spec.hasAuth()) {
                this.lastResult = { ok: false, reason: '没有登录凭证' };
                if (!(opts && opts.silent)) {
                    alert(spec.noAuthHint ||
                        '还没有捕获到登录凭证。\n\n请先打开一个对话（或刷新页面），等历史加载出来后再点此按钮。');
                }
                return;
            }

            const originalText = '📚 全部会话';
            this.running = true;
            this.stopRequested = false;
            spec.setBusy(true);
            button.style.backgroundColor = '#dc3545';
            logger.info(`开始${spec.label}全部会话批量导出...`);

            try {
                // 第一步：拉取全部会话列表
                const conversations = await spec.fetchList((count) => {
                    button.innerText = `⏳ 会话列表 ${count}...`;
                });
                if (!conversations.length) {
                    this.lastResult = { ok: false, reason: '没有获取到任何会话' };
                    if (!(opts && opts.silent)) {
                        alert(`没有获取到任何会话。\n请确认已登录${spec.label}且会话列表非空，然后刷新页面重试。`);
                    }
                    return;
                }
                const ordered = spec.sortList ? spec.sortList(conversations) : conversations;
                logger.info(`${spec.label}批量导出：共 ${ordered.length} 个会话`);

                // 自动模式(opts.filter)：只导出符合条件的会话；否则超过50个让用户选择范围
                let toExport = ordered;
                if (opts && typeof opts.filter === 'function') {
                    toExport = ordered.filter(opts.filter);
                    if (!toExport.length) {
                        this.lastResult = { ok: false, reason: '没有符合条件的会话' };
                        logger.info('自动模式：所选日期没有符合条件的会话，跳过');
                        return;
                    }
                    logger.info(`自动模式：筛选出 ${toExport.length} 个符合条件的会话`);
                } else if (ordered.length > 50) {
                    const ok = confirm(`共找到 ${ordered.length} 个会话。\n\n` +
                        `全部导出需要逐个拉取历史，可能耗时很久（可随时再次点击按钮停止）。\n\n` +
                        `【确定】导出全部\n【取消】只导出最近的 50 个`);
                    if (!ok) toExport = ordered.slice(0, 50);
                }

                // 第二步：逐个拉取完整历史并转换为Markdown
                const allMd = [];
                const allJson = [];
                const failed = [];
                const totals = { convTurns: 0, totalChars: 0, totalTokens: 0, questions: 0, summary: '0 条消息' };

                for (let i = 0; i < toExport.length; i++) {
                    if (this.stopRequested) break;
                    const conv = toExport[i];
                    const title = spec.convTitle(conv);
                    button.innerText = `⏳ ${i + 1}/${toExport.length} ${title}`.slice(0, 22);

                    try {
                        const messages = await spec.fetchMessages(conv);
                        if (messages.length) {
                            spec.accumulate(totals, messages);
                            const convData = spec.buildConvData(conv, messages, title);
                            allMd.push(await spec.convert(convData, title));
                            allJson.push(spec.buildJson(conv, messages, title));
                        }
                    } catch (e) {
                        logger.error(`拉取会话失败: ${title}`, e);
                        failed.push({ title, error: e.message });
                    }
                    await this.sleep(250);
                }

                if (!allJson.length) {
                    this.lastResult = { ok: false, reason: '所有会话拉取失败' };
                    if (!(opts && opts.silent)) alert('没有成功导出任何会话。' +
                        (failed.length ? `\n失败示例: ${failed[0].title} - ${failed[0].error}` : ''));
                    return;
                }

                // 第三步：填充导出状态，复用现有的 JSON/MD/Word 按钮
                state.platformType = spec.platformType;
                state.lastUpdateTime = new Date().toLocaleTimeString();
                state.currentTitle = `${spec.label}全部${allJson.length}个会话`;
                state.messageStats = {
                    totalTokens: totals.totalTokens || 0,
                    totalChars: totals.totalChars || 0,
                    fileCount: 0,
                    questions: totals.questions || 0,
                    convTurns: totals.convTurns || 0
                };
                state.targetResponse = JSON.stringify({
                    batch_export: true,
                    platform: spec.platformType,
                    conversation_count: allJson.length,
                    conversations: allJson
                });
                state.convertedMd = `# ${spec.label}全部会话导出（共 ${allJson.length} 个会话）\n\n` + allMd.join('\n\n');
                ui.updateButtonStatus();
                this.lastResult = { ok: true, count: allJson.length };

                const stopNote = this.stopRequested ? '（已提前停止）' : '';
                const failNote = failed.length
                    ? `\n失败 ${failed.length} 个: ${failed.map(f => f.title).slice(0, 5).join('、')}${failed.length > 5 ? ' 等' : ''}`
                    : '';
                if (!(opts && opts.silent)) {
                    autoSaver.autoSaveIfNeeded(`${spec.label}批量导出`);
                    alert(`${spec.label}批量导出完成${stopNote}：\n成功 ${allJson.length} 个会话 / ${totals.summary}` +
                        failNote + '\n\n扩展面板开启自动保存后，文件会自动存入设定的目录；\n也可点击 JSON / MD / Word 按钮手动保存。');
                }
                logger.info(`${spec.label}批量导出完成: ${allJson.length} 会话 / ${failed.length} 失败`);
            } catch (e) {
                logger.error(`${spec.label}批量导出失败`, e);
                this.lastResult = { ok: false, reason: e.message };
                if (!(opts && opts.silent)) {
                    alert(`${spec.label}批量导出失败: ${e.message}\n请刷新页面后重试。`);
                }
            } finally {
                this.running = false;
                this.stopRequested = false;
                spec.setBusy(false);
                button.innerText = originalText;
                button.style.backgroundColor = '#007bff';
            }
        }
        };
    };

    // ============ DeepSeek"导出全部最近会话"模块 ============
    // 通过直接重放DeepSeek接口拉取全部会话：
    // 1. GET /api/v0/chat_session/fetch_page 分页枚举会话（lte_cursor.value=上页最旧会话的seq_id）
    // 2. GET /api/v0/chat/history_messages?chat_session_id=... 分页拉取每个会话的完整历史
    // 鉴权：Authorization: Bearer <userToken> + x-device-id，均从拦截到的真实请求动态捕获
    function captureDeepseekAuthHeader(header, value) {
        if (!value) return;
        const h = String(header).toLowerCase();
        if (h === 'authorization') state.deepseekAuth.token = value;
        else if (h === 'x-device-id') state.deepseekAuth.deviceId = value;
    }

    const deepseekBatchExporter = genericBatchExporter({
        label: 'DeepSeek',
        platformType: 'deepseek',
        // 优先用拦截到的真实请求头；userToken 未捕获时从 localStorage 兜底读取
        hasAuth: () => {
            if (!state.deepseekAuth.token) {
                try {
                    const t = localStorage.getItem('userToken');
                    if (t) state.deepseekAuth.token = /^Bearer /i.test(t) ? t : 'Bearer ' + t;
                } catch (e) { /* 忽略 */ }
            }
            return !!(state.deepseekAuth.token && state.deepseekAuth.deviceId);
        },
        setBusy: (on) => { state.deepseekBatchBusy = on; },

        apiHeaders: () => ({
            'Authorization': state.deepseekAuth.token,
            'x-device-id': state.deepseekAuth.deviceId,
            'Accept': 'application/json, text/plain, */*'
        }),

        // 枚举全部会话：首页不带游标，之后 lte_cursor.value=本页最旧（seq_id最小）会话的seq_id，
        // 直到 has_more=false（has_more 位于 data.biz_data 层）
        fetchList: async function(onProgress) {
            const sessions = [];
            const seen = new Set();
            let cursorValue;
            let hasMore = true;
            let pages = 0;

            while (hasMore && pages < 100) {
                let url = 'https://chat.deepseek.com/api/v0/chat_session/fetch_page?count=50&lte_cursor.pinned=false';
                if (cursorValue !== undefined) url += `&lte_cursor.value=${cursorValue}`;
                const resp = await withBatchRetry(() => fetch(url, {
                    headers: this.apiHeaders(),
                    credentials: 'include'
                }), 'DeepSeek会话列表');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const j = await resp.json();
                if (j.code !== 0) throw new Error(`code ${j.code}: ${j.msg || '接口错误'}`);

                const biz = j.data?.biz_data || {};
                const list = biz.chat_sessions || [];
                for (const s of list) {
                    if (s.id && !seen.has(String(s.id))) {
                        seen.add(String(s.id));
                        sessions.push(s);
                    }
                }
                hasMore = !!biz.has_more;
                pages++;
                onProgress?.(sessions.length);
                if (hasMore && list.length) {
                    const last = list[list.length - 1];
                    if (last?.seq_id === undefined || String(last.seq_id) === String(cursorValue)) break; // 停滞保护
                    cursorValue = last.seq_id;
                    await this.sleep(200);
                } else if (hasMore) break;
            }
            return sessions;
        },

        sortList: (list) => [...list].sort((a, b) =>
            String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? ''))),
        convTitle: (s) => s.title || `未命名会话_${s.id}`,

        // 拉取单个会话完整历史：第一页不带message_id（返回最新一段），其后传当前已收集中
        // 最旧消息的message_id取更早一段。注意：message_id随时间递减（最新消息id最小），
        // 按message_id去重，某页无新增即已到最早
        fetchMessages: async function(s) {
            const map = new Map();
            let cursor;
            let pages = 0;

            while (pages < 200) {
                let url = `https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id=${s.id}&count=50`;
                if (cursor !== undefined) url += `&message_id=${cursor}`;
                const resp = await withBatchRetry(() => fetch(url, {
                    headers: this.apiHeaders(),
                    credentials: 'include'
                }), 'DeepSeek消息');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const j = await resp.json();
                if (j.code !== 0) throw new Error(`code ${j.code}: ${j.msg || '接口错误'}`);

                const list = j.data?.biz_data?.chat_messages || [];
                if (!list.length) break;
                let added = 0;
                for (const m of list) {
                    const id = m.message_id ?? m.id ?? m.inserted_at;
                    if (id !== undefined && id !== null && !map.has(String(id))) {
                        map.set(String(id), m);
                        added++;
                    }
                }
                pages++;
                if (added === 0) break; // 无新增消息，已到最早

                // 找当前收集里message_id数值最大（最旧）的消息作为下次游标
                let oldestId = null;
                for (const m of map.values()) {
                    const n = Number(m.message_id ?? m.id);
                    if (!isNaN(n) && (oldestId === null || n > oldestId)) oldestId = n;
                }
                if (oldestId === null || String(oldestId) === String(cursor)) break; // 停滞保护
                cursor = oldestId;
                await this.sleep(150);
            }
            return [...map.values()].sort((a, b) => compareSortKeys(a.inserted_at, b.inserted_at));
        },

        // 构造与拦截响应一致的结构，复用现有DeepSeek转换器
        buildConvData: (s, messages, title) => ({
            data: { biz_data: { chat_session: { title: title }, chat_messages: messages } }
        }),
        convert: (convData) => platformConverters.deepseek(convData),
        accumulate: (totals, messages) => {
            totals.convTurns += messages.length;
            totals.totalTokens += messages.reduce((acc, m) => acc + (m.accumulated_token_usage || 0), 0);
            totals.summary = `${totals.convTurns} 条消息`;
        },
        buildJson: (s, messages, title) => ({
            chat_session_id: String(s.id),
            title: title,
            updated_at: s.updated_at,
            pinned: !!s.pinned,
            message_count: messages.length,
            messages
        })
    });
    // ============ DeepSeek批量导出模块结束 ============

    // ============ 元宝"导出全部最近会话"模块 ============
    // 通过直接重放元宝接口拉取全部会话：
    // 1. POST /api/user/agent/conversation/v3/list 分页枚举会话（last_replied_at游标）
    // 2. GET /api/user/agent/conversation/v1/detail?conversation_id=... 拉取会话消息；
    //    超长会话（收集数 < 列表给的maxIndex）时自适应探测分页参数（last_id/last_index等）
    // 鉴权头与body结构从拦截到的真实请求动态捕获（X-Source/T-UserID/X-ID/X-device-id等）
    const yuanbaoBatchExporter = genericBatchExporter({
        label: '元宝',
        platformType: 'yuanbao',
        hasAuth: () => !!(state.yuanbaoListReq && state.yuanbaoListReq.headers),
        noAuthHint: '还没有捕获到元宝的请求凭证。\n\n请先在元宝页面上下滚动一下左侧"最近"会话列表，或点开任意一个历史对话（触发一次列表请求），然后再重新上传。',
        setBusy: (on) => { state.yuanbaoBatchBusy = on; },

        listBody: function() {
            const raw = state.yuanbaoListReq?.body;
            if (!raw) return {};
            try { return typeof raw === 'string' ? JSON.parse(raw) : { ...raw }; }
            catch (e) { return {}; }
        },

        // 枚举全部会话：以捕获到的body为模板（page_size放大），last_replied_at=上页最后一条
        fetchList: async function(onProgress) {
            const sessions = [];
            const seen = new Set();
            let lastRepliedAt;
            let total = Infinity;
            let pages = 0;

            while (sessions.length < total && pages < 100) {
                const body = { ...this.listBody(), page_size: 40 };
                // 首页 last_replied_at=0（与页面真实请求一致，缺该字段可能被服务端判参数错误）
                body.last_replied_at = lastRepliedAt === undefined ? 0 : lastRepliedAt;

                const resp = await withBatchRetry(() => fetch('https://yuanbao.tencent.com/api/user/agent/conversation/v3/list', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...(state.yuanbaoListReq?.headers || {}) },
                    body: JSON.stringify(body),
                    credentials: 'include'
                }), '元宝会话列表');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const j = await resp.json();
                const data = j.data || {};
                const list = data.list || [];
                for (const s of list) {
                    const key = String(s.id ?? s.conversationId ?? '');
                    if (key && !seen.has(key)) {
                        seen.add(key);
                        sessions.push(s);
                    }
                }
                if (data.total_count !== undefined) total = Number(data.total_count);
                pages++;
                onProgress?.(sessions.length);
                if (!list.length) break;

                const last = list[list.length - 1];
                const next = last?.lastRepliedAt ?? last?.last_replied_at;
                if (next === undefined || String(next) === String(lastRepliedAt)) break; // 停滞保护
                lastRepliedAt = next;
                await this.sleep(200);
            }
            return sessions;
        },

        sortList: (list) => list, // 接口本身按最近回复降序返回
        convTitle: (s) => s.title || `未命名会话_${s.id}`,

        // 拉取单个会话的完整消息（v1/detail 已从 GET 改为 POST，2026-10-22 实测）：
        // POST body {conversationId, agentId, offset, limit}，响应顶层即会话对象（convs 数组）
        // 分页为 index 游标：offset=0 从最新开始取倒序 limit 条；翻页 offset=本页最小 index
        // （取 index < offset 的消息），hasMore=true 表示还有更早消息
        fetchMessages: async function(s) {
            const convId = String(s.id ?? s.conversationId ?? '');
            const agentId = String(s.agentId || '');
            const headers = Object.assign({ 'Content-Type': 'application/json' }, state.yuanbaoListReq?.headers || {});
            const collected = new Map();
            const addAll = (arr) => {
                let n = 0;
                for (const c of (arr || [])) {
                    const k = String(c.id ?? `${c.index}|${c.speaker}`);
                    if (!collected.has(k)) {
                        collected.set(k, c);
                        n++;
                    }
                }
                return n;
            };
            const LIMIT = 50;
            let offset = 0, guard = 0;
            while (guard < 200) {
                guard++;
                const j = await withBatchRetry(() => fetch('https://yuanbao.tencent.com/api/user/agent/conversation/v1/detail', {
                    method: 'POST',
                    headers: headers,
                    credentials: 'include',
                    body: JSON.stringify({ conversationId: convId, agentId: agentId, offset: offset, limit: LIMIT })
                }).then(resp => {
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    return resp.json();
                }), '元宝会话消息');
                const page = j.convs || [];
                if (!page.length) break;
                addAll(page);
                offset = Math.min(...page.map(c => Number(c.index ?? 0)));
                if (j.hasMore === false || page.length < LIMIT) break;
                await this.sleep(150);
            }
            return [...collected.values()].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
        },

        // 构造与拦截响应一致的结构，复用现有元宝转换器
        buildConvData: (s, convs, title) => ({ convs: convs, sessionTitle: title }),
        convert: (convData) => platformConverters.yuanbao(convData),
        accumulate: (totals, convs) => {
            totals.convTurns += convs.length;
            totals.totalChars += (convs || []).reduce((acc, c) => {
                let n = acc;
                (c.speechesV2 || []).forEach(sp => (sp.content || []).forEach(b => { if (b.msg) n += b.msg.length; }));
                if (Array.isArray(c.content)) c.content.forEach(b => { if (b.msg) n += b.msg.length; });
                else if (typeof c.content === 'string') n += c.content.length;
                return n;
            }, 0);
            totals.summary = `${totals.convTurns} 条消息`;
        },
        buildJson: (s, convs, title) => ({
            conversation_id: String(s.id),
            title: title,
            last_replied_at: s.lastRepliedAt,
            max_index: s.maxIndex,
            message_count: convs.length,
            convs
        })
    });
    // ============ 元宝批量导出模块结束 ============

    // ============ Kimi"导出全部最近会话"模块 ============
    // 通过直接重放新版 Kimi 的 gRPC-Gateway 接口拉取全部会话：
    // 1. POST /apiv2/kimi.gateway.feed.v1.FeedService/ListFeeds 枚举会话（page_token 游标翻页）
    // 2. POST /apiv2/kimi.gateway.chat.v1.ChatService/ListMessages 拉取每个会话的完整消息
    // 鉴权：Authorization: Bearer <localStorage.access_token>（JWT 短时效，每次请求前现读）；
    // x-msh-* 系列头经重放验证均可省略
    const kimiBatchExporter = genericBatchExporter({
        label: 'Kimi',
        platformType: 'kimi',
        setBusy: (on) => { state.kimiBatchBusy = on; },

        // JWT 存在 localStorage 且约15分钟过期，每次请求前现读（过期时明确提示刷新页面）
        apiHeaders: () => {
            let token = '';
            try { token = localStorage.getItem('access_token') || state.authToken || ''; } catch (e) { /* 沙箱环境兜底 */ }
            if (token && !/^Bearer /i.test(token)) token = 'Bearer ' + token;
            return { 'Authorization': token, 'Content-Type': 'application/json' };
        },

        post: async function(url, body, label) {
            const resp = await withBatchRetry(() => fetch(url, {
                method: 'POST',
                headers: this.apiHeaders(),
                body: JSON.stringify(body)
            }), label);
            if (resp.status === 401) throw new Error('登录凭证已过期（401），请刷新Kimi页面后重试');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return resp.json();
        },

        // 枚举全部会话：page_size=100，page_token 游标翻页，直到响应无 nextPageToken
        fetchList: async function(onProgress) {
            const chats = [];
            const seen = new Set();
            let pageToken;
            let pages = 0;
            while (pages < 100) {
                const body = { page_size: 100 };
                if (pageToken) body.page_token = pageToken;
                const j = await this.post('https://www.kimi.com/apiv2/kimi.gateway.feed.v1.FeedService/ListFeeds',
                    body, 'Kimi会话列表');
                for (const item of (j.items || [])) {
                    const c = item?.chat;
                    if (c?.id && !seen.has(String(c.id))) {
                        seen.add(String(c.id));
                        chats.push(c);
                    }
                }
                pages++;
                onProgress?.(chats.length);
                if (!j.nextPageToken) break;
                pageToken = j.nextPageToken;
                await this.sleep(200);
            }
            return chats;
        },

        sortList: (list) => list, // 接口按 updateTime 倒序返回
        convTitle: (c) => c.name || `未命名会话_${c.id}`,

        // 拉取单个会话完整消息：page_token 游标翻页；消息最新在前，
        // 过滤根 system 消息（id==chatId）后按 createTime 正序返回
        fetchMessages: async function(c) {
            const messages = [];
            const seen = new Set();
            let pageToken;
            let pages = 0;
            while (pages < 200) {
                const body = { chatId: String(c.id), page_size: 100 };
                if (pageToken) body.page_token = pageToken;
                const j = await this.post('https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/ListMessages',
                    body, 'Kimi消息');
                for (const m of (j.messages || [])) {
                    if (m.id && !seen.has(String(m.id))) {
                        seen.add(String(m.id));
                        messages.push(m);
                    }
                }
                pages++;
                if (!j.nextPageToken) break;
                pageToken = j.nextPageToken;
                await this.sleep(150);
            }
            return messages
                .filter(m => m.role !== 'system' && String(m.id) !== String(c.id))
                .sort((a, b) => String(a.createTime ?? '').localeCompare(String(b.createTime ?? '')));
        },

        // 构造与转换器一致的输入结构（messages 的 blocks 含 text/think 块）
        buildConvData: (c, messages, title) => ({ messages: messages, chat: c }),
        convert: (convData, title) => platformConverters.kimi(convData, title),
        accumulate: (totals, messages) => {
            totals.convTurns += messages.length;
            totals.totalChars += messages.reduce((acc, m) =>
                acc + JSON.stringify(m.blocks || []).length, 0);
            totals.summary = `${totals.convTurns} 条消息`;
        },
        buildJson: (c, messages, title) => ({
            chat_id: String(c.id),
            title: title,
            create_time: c.createTime,
            update_time: c.updateTime,
            message_count: messages.length,
            messages
        })
    });
    // ============ Kimi批量导出模块结束 ============

    // ============ 讯飞星火"导出全部最近会话"模块 ============
    // 通过直接重放星火接口拉取全部会话：
    // 1. POST /iflygpt/u/chat-list/v2/chat-list 枚举会话（chatType 1=普通对话 2=智能体会话，一次全量无分页）
    // 2. GET /iflygpt/u/chat_history/all/{id} 拉取单个会话完整历史（同样无分页，纯Cookie鉴权）
    // 注意：接口出错时仍返回 HTTP 200，成功判定必须看 flag===true && code===0；
    // POST 缺 Content-Type 头会报 99999
    const iflytekBatchExporter = genericBatchExporter({
        label: '星火',
        platformType: 'iflytek',
        setBusy: (on) => { state.iflytekBatchBusy = on; },

        post: async function(url, body, label) {
            const resp = await withBatchRetry(() => fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }, // 缺此头服务端报 99999
                body: JSON.stringify(body),
                credentials: 'include'
            }), label);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const j = await resp.json();
            if (j.flag !== true || j.code !== 0) {
                throw new Error(j.desc || `code ${j.code}`);
            }
            return j;
        },

        // 枚举全部会话：普通对话(chatType:1) + 智能体会话(chatType:2) 各拉一次合并去重
        fetchList: async function(onProgress) {
            const chats = [];
            const seen = new Set();
            for (const chatType of [1, 2]) {
                const j = await this.post('https://xinghuo.xfyun.cn/iflygpt/u/chat-list/v2/chat-list',
                    { chatType: chatType }, '星火会话列表');
                for (const s of (j.data || [])) {
                    if (s.id !== undefined && !seen.has(String(s.id))) {
                        seen.add(String(s.id));
                        chats.push(s);
                    }
                }
                onProgress?.(chats.length);
                await this.sleep(200);
            }
            return chats;
        },

        sortList: (list) => [...list].sort((a, b) =>
            String(b.updateTime ?? '').localeCompare(String(a.updateTime ?? ''))),
        convTitle: (s) => s.title || `未命名会话_${s.id}`,

        // 拉取单个会话完整历史：一次全量返回，无分页
        fetchMessages: async function(s) {
            const resp = await withBatchRetry(() => fetch(
                `https://xinghuo.xfyun.cn/iflygpt/u/chat_history/all/${s.id}`,
                { credentials: 'include' }), '星火消息');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const j = await resp.json();
            if (j.flag !== true || j.code !== 0) throw new Error(j.desc || `code ${j.code}`);
            const entry = (j.data || [])[0] || {};
            return entry.historyList || [];
        },

        // 构造与拦截响应一致的结构，复用现有星火转换器
        buildConvData: (s, historyList, title) => ({ data: [{ chatId: s.id, historyList: historyList }] }),
        convert: (convData, title) => platformConverters.iflytek(convData, title),
        accumulate: (totals, historyList) => {
            totals.convTurns += historyList.length;
            totals.totalChars += historyList.reduce((acc, m) =>
                acc + (m.message?.length || 0) + (m.answer?.length || 0), 0);
            totals.summary = `${totals.convTurns} 条消息`;
        },
        buildJson: (s, historyList, title) => ({
            chat_id: s.id,
            title: title,
            create_time: s.createTime,
            update_time: s.updateTime,
            bot_id: s.botId ?? undefined,
            message_count: historyList.length,
            historyList
        })
    });
    // ============ 星火批量导出模块结束 ============

    // UI相关功能
    const ui = {
        updateButtonStatus: () => {
            const jsonButton = document.getElementById('downloadJsonButton');
            const mdButton = document.getElementById('downloadMdButton');
            const wordButton = document.getElementById('downloadWordButton');

            if (!jsonButton || !mdButton || !wordButton) return;

            const hasResponse = state.targetResponse !== null;
            let platformName = 'AI对话';
            let statsText = '';

            try {
                const jsonData = JSON.parse(state.targetResponse || '{}');

                switch(state.platformType) {
                    case 'deepseek':
                        platformName = `DeepSeek_${jsonData.data?.biz_data?.chat_session?.title || 'deepseek-Chat'}`;
                        statsText = `Token用量: ${state.messageStats.totalTokens}`;
                        break;
                    case 'ncn':
                        platformName = `AI-N_${jsonData.data?.title || 'AI-Chat'}`;
                        statsText = `字数: ${state.messageStats.totalChars} | 附件: ${state.messageStats.fileCount}`;
                        break;
                    case 'yuanbao':
                        platformName = `Yuanbao_${jsonData.sessionTitle || jsonData.title || 'Yuanbao-Chat'}`;
                        statsText = `对话轮次: ${state.messageStats.convTurns} | 文件: ${state.messageStats.fileCount}`;
                        break;
                    case 'kimi':
                        platformName = `Kimi_${state.currentTitle}`;
                        statsText = `消息数: ${state.messageStats.convTurns} | 字数: ${state.messageStats.totalChars}`;
                        break;
                    case 'tongyi':
                        platformName = `Tongyi_${state.currentTitle}`;
                        statsText = `对话轮次: ${state.messageStats.convTurns} | 字数: ${state.messageStats.totalChars}`;
                        break;
                    case 'qianwen':
                        platformName = `Qianwen_${state.currentTitle}`;
                        statsText = `对话轮次: ${state.messageStats.convTurns} | 字数: ${state.messageStats.totalChars}`;
                        break;
                    case 'iflytek':
                        platformName = `xinghuo_${state.currentTitle}`;
                        statsText = `对话轮次: ${state.messageStats.convTurns} | 字数: ${state.messageStats.totalChars}`;
                        break;
                    case 'doubao':
                    case 'doubao_title':
                        platformName = state.currentTitle ||jsonData.downlink_body?.get_conv_info_downlink_body?.conversation_info?.name ||'豆包对话';
                        statsText = state.platformType === 'doubao_title'
                            ? `对话数: ${state.messageStats.convTurns}`
                        : `消息数: ${state.messageStats.convTurns} | 字数: ${state.messageStats.totalChars} | 附件: ${state.messageStats.fileCount}`;
                        break;
                }
            } catch (e) {
                logger.error('更新按钮状态时解析JSON失败', e);
            }

            // 更新按钮状态和样式
            [jsonButton, mdButton, wordButton].forEach(button => {
                button.style.backgroundColor = hasResponse ? '#28a745' : '#007bff';
                button.dataset.tooltip = `${platformName}数据已就绪\n${statsText}\n最后更新: ${state.lastUpdateTime}`;

                // 悬停效果
                button.onmouseenter = () => {
                    button.style.transform = 'translateY(-2px)';
                    button.style.boxShadow = '0 6px 8px rgba(0,0,0,0.15)';
                };
                button.onmouseleave = () => {
                    button.style.transform = '';
                    button.style.boxShadow = '0 2px 5px rgba(0,0,0,0.2)';
                };
            });

            // 特殊处理MD和Word按钮
            mdButton.style.backgroundColor = state.convertedMd ? '#28a745' : '#007bff';
            wordButton.style.backgroundColor = state.convertedMd ? '#28a745' : '#007bff';

            // 禁用状态
            jsonButton.disabled = !hasResponse;
            mdButton.disabled = !state.convertedMd;
            wordButton.disabled = !state.convertedMd;
        },

        createDownloadButtons: () => {
            // 如果按钮已存在，则不再创建
            if (document.getElementById('downloadJsonButton')) return;

            const buttonContainer = document.createElement('div');
            buttonContainer.id = 'exportButtonsContainer';
            const loadAllButton = document.createElement('button');
            const jsonButton = document.createElement('button');
            const mdButton = document.createElement('button');
            const wordButton = document.createElement('button');
            const uploadButton = document.createElement('button');

            // 容器样式
            Object.assign(buttonContainer.style, {
                position: 'fixed',
                top: '45%',
                right: '10px',
                zIndex: '9999',
                display: 'flex',
                flexDirection: 'column',
                gap: '10px',
                opacity: CONFIG.BUTTON_OPACITY,
                transition: 'opacity 0.3s ease',
                cursor: 'move'
            });

            // 按钮通用样式
            const buttonStyles = {
                padding: '8px 12px',
                backgroundColor: '#007bff',
                color: '#ffffff',
                border: 'none',
                borderRadius: '5px',
                cursor: 'pointer',
                transition: 'all 0.3s ease',
                fontFamily: 'Arial, sans-serif',
                boxShadow: '0 2px 5px rgba(0,0,0,0.2)',
                whiteSpace: 'nowrap',
                fontSize: '14px'
            };

            // 设置按钮属性和样式
            loadAllButton.id = 'loadAllHistoryButton';
            loadAllButton.innerText = '⏫ 加载全部';
            loadAllButton.title = '自动向上滚动，把被隐藏的历史轮次全部加载出来';
            jsonButton.id = 'downloadJsonButton';
            jsonButton.innerText = 'JSON';
            mdButton.id = 'downloadMdButton';
            mdButton.innerText = 'MD';
            wordButton.id = 'downloadWordButton';
            wordButton.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 5px;">
                    <path d="M4 4.5V19C4 20.1046 4.89543 21 6 21H18C19.1046 21 20 20.1046 20 19V8.2468C20 7.61538 19.7893 7.00372 19.4029 6.5L16.5 3H6C4.89543 3 4 3.89543 4 5V5.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
                    <path d="M16 3V7C16 7.55228 16.4477 8 17 8H20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
                    <path d="M8 13L10 17L12 13L14 17L16 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
                </svg>Word`;
            uploadButton.id = 'uploadToDbButton';
            uploadButton.innerText = '⇧ 上传';
            uploadButton.title = '把当前已捕获的对话（或批量导出的全部会话）上传到数据库';
            uploadButton.style.backgroundColor = '#6f42c1';
            // 一键拉取"最近"列表全部会话并合并导出（除纳米AI/旧版通义外的六平台支持）
            const batchHostname = location.hostname;
            const batchPlatform = batchHostname.includes('doubao') ? 'doubao'
                : batchHostname.includes('qianwen') ? 'qianwen'
                : batchHostname.includes('deepseek') ? 'deepseek'
                : batchHostname.includes('yuanbao') ? 'yuanbao'
                : batchHostname.includes('kimi') ? 'kimi'
                : batchHostname.includes('xfyun') ? 'iflytek'
                : null;
            let allConvButton = null;
            if (batchPlatform) {
                allConvButton = document.createElement('button');
                allConvButton.id = 'downloadAllSessionsButton';
                allConvButton.innerText = '📚 全部会话';
                allConvButton.title = '拉取左侧"最近"列表中所有会话的完整历史，合并为一个文件导出';
                Object.assign(allConvButton.style, buttonStyles);
            }

            // 添加按钮的 classname
            [loadAllButton, jsonButton, mdButton, wordButton, uploadButton].forEach(btn => {
                btn.className = 'export-button';
            });
            if (allConvButton) allConvButton.className = 'export-button';

            Object.assign(loadAllButton.style, buttonStyles);
            Object.assign(jsonButton.style, buttonStyles);
            Object.assign(mdButton.style, buttonStyles);
            Object.assign(wordButton.style, buttonStyles);
            Object.assign(uploadButton.style, buttonStyles);

            // 鼠标悬停效果
            buttonContainer.onmouseenter = () =>{
                buttonContainer.style.opacity = CONFIG.BUTTON_HOVER_OPACITY;
                buttonContainer.style.boxShadow = '0 8px 16px rgba(0,0,0,0.15)';
            };
            buttonContainer.onmouseleave = () =>{
                buttonContainer.style.opacity = CONFIG.BUTTON_OPACITY;
                buttonContainer.style.boxShadow = '0 4px 12px rgba(0,0,0,0.1)';
            }


            // 拖动功能
            let isDragging = false;
            let initialX, initialY, xOffset = 0, yOffset = 0;

            buttonContainer.addEventListener('mousedown', (e) => {
                initialX = e.clientX - xOffset;
                initialY = e.clientY - yOffset;
                if (e.target === buttonContainer) {
                    isDragging = true;
                }
            });

            document.addEventListener('mousemove', (e) => {
                if (isDragging) {
                    e.preventDefault();
                    xOffset = e.clientX - initialX;
                    yOffset = e.clientY - initialY;
                    buttonContainer.style.transform = `translate(${xOffset}px, ${yOffset}px)`;
                }
            });

            document.addEventListener('mouseup', () => {
                isDragging = false;
            });

            // 阻止按钮的 mousedown 冒泡影响拖动
            [loadAllButton, jsonButton, mdButton, wordButton, uploadButton].forEach(btn => {
                btn.addEventListener('mousedown', (e) => e.stopPropagation());
            });
            if (allConvButton) {
                allConvButton.addEventListener('mousedown', (e) => e.stopPropagation());
                allConvButton.onclick = () => {
                    const exporters = {
                        doubao: doubaoBatchExporter,
                        qianwen: qianwenBatchExporter,
                        deepseek: deepseekBatchExporter,
                        yuanbao: yuanbaoBatchExporter,
                        kimi: (typeof kimiBatchExporter !== 'undefined') ? kimiBatchExporter : null,
                        iflytek: (typeof iflytekBatchExporter !== 'undefined') ? iflytekBatchExporter : null
                    };
                    const exporter = exporters[batchPlatform];
                    if (exporter) exporter.start(allConvButton);
                };
            }

            // 按钮点击事件
            loadAllButton.onclick = () => autoScroller.start(loadAllButton);
            jsonButton.onclick = exportHandlers.json;
            mdButton.onclick = exportHandlers.markdown;
            wordButton.onclick = exportHandlers.word;
            uploadButton.onclick = () => uploader.start(uploadButton);

            // 组装按钮
            buttonContainer.appendChild(loadAllButton);
            if (allConvButton) buttonContainer.appendChild(allConvButton);
            buttonContainer.appendChild(jsonButton);
            buttonContainer.appendChild(mdButton);
            buttonContainer.appendChild(wordButton);
            buttonContainer.appendChild(uploadButton);
            document.body.appendChild(buttonContainer);

            // 自动折叠/展开功能
            let isCollapsed = false;
            let collapseTimeout;
            const collapseDelay = 2000; // 2秒后自动折叠

            // 自动折叠函数
            const autoCollapse = () => {
                collapseTimeout = setTimeout(() => {
                    // 自动滚动/各平台批量导出/数据库上传进行中时保持展开，方便查看进度
                    if (autoScroller.running || doubaoBatchExporter.running || qianwenBatchExporter.running ||
                        deepseekBatchExporter.running || yuanbaoBatchExporter.running ||
                        kimiBatchExporter.running || iflytekBatchExporter.running || uploader.running) {
                        autoCollapse();
                        return;
                    }
                    buttonContainer.classList.add('collapsed');
                    isCollapsed = true;
                }, collapseDelay);
            };

            // 初始设置自动折叠
            autoCollapse();

            // 鼠标进入展开
            buttonContainer.addEventListener('mouseenter', () => {
                clearTimeout(collapseTimeout);
                if (isCollapsed) {
                    buttonContainer.classList.remove('collapsed');
                    isCollapsed = false;
                }
            });

            // 鼠标离开后延迟折叠
            buttonContainer.addEventListener('mouseleave', () => {
                if (!isCollapsed) {
                    autoCollapse();
                }
            });

            // 点击折叠按钮也可展开
            buttonContainer.addEventListener('click', (e) => {
                if (isCollapsed && e.target === buttonContainer) {
                    buttonContainer.classList.remove('collapsed');
                    isCollapsed = false;
                    clearTimeout(collapseTimeout);
                }
            });

            // 确保拖动时不折叠
            buttonContainer.addEventListener('mousedown', () => {
                clearTimeout(collapseTimeout);
            });

            // 添加加载动画样式
            GM_addStyle(`
               .export-button {
                transition: all 0.2s ease;
                }
                .export-button:hover {
                background-color: #0056b3 !important;
               }
                .export-button:disabled {
                    opacity: 0.6;
                    cursor: not-allowed;
                    background-color: #6c757d !important;
                }
                .gm-spinner {
                    border: 2px solid rgba(255,255,255,0.3);
                    border-radius: 50%;
                    border-top-color: #fff;
                    width: 12px;
                    height: 12px;
                    animation: spin ${CONFIG.SPINNER_ANIMATION_DURATION} linear infinite;
                    display: inline-block;
                    margin-right: 8px;
                    vertical-align: middle;
                }
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
                    /* 添加折叠/展开相关样式 */
                #exportButtonsContainer.collapsed {
                    width: 40px;
                    height: 40px;
                    overflow: hidden;
                    padding: 5px;
                }

                #exportButtonsContainer.collapsed .export-button {
                    display: none;
                }

                #exportButtonsContainer.collapsed::before {
                    content: "↓↓↓";
                    position: absolute;
                    width: 30px;
                    height: 30px;
                    background: #007bff;
                    color: white;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 16px;

                    cursor: pointer;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.2);
                }

                #exportButtonsContainer:not(.collapsed)::before {
                    display: none;
                }

                #exportButtonsContainer {
                    transition: all 0.3s ease;
                }
                /* 标题相关样式 */
                [data-tooltip] {
                    position: relative;
                }

                [data-tooltip]:hover::after {
                    content: attr(data-tooltip);
                    position: absolute;
                    right: 100%;
                    top: 50%;
                    transform: translateY(-50%);
                    background:rgba(228,17,136,0.7);
                    color: #fff;
                    padding: 8px 12px;
                    border-radius: 4px;
                    font-size: 16px;
                    font-family: "方正小标宋简体","黑体", Arial, sans-serif;
                    white-space: pre;
                    margin-right: 10px;
                    pointer-events: none;
                    opacity: 0.1;
                    transition: opacity 0.3s;
                }

                [data-tooltip]:hover::after {
                    opacity: 1;
                }
            `);

            ui.updateButtonStatus();
        }
    };

    // 网络拦截
    function setupNetworkInterception() {
        // 千问（qianwen.com）等站点的 CSP 含 nonce + 'Strict-Dynamic'，可能阻止篡改猴
        // 把脚本注入页面主环境，脚本会退到沙箱/隔离环境运行：此时 DOM 操作（悬浮按钮）正常，
        // 但直接改写 window.fetch 钩不到页面的真实请求。因此优先挂钩页面真实 window（unsafeWindow）。
        const sandboxed = (typeof unsafeWindow !== 'undefined' && unsafeWindow !== window);
        const pageWindow = sandboxed ? unsafeWindow : window;
        if (sandboxed) {
            logger.warn('当前运行在沙箱环境，改用 unsafeWindow 挂钩网络请求');
        }

        // 拦截XHR请求
        const originalOpen = pageWindow.XMLHttpRequest.prototype.open;
        pageWindow.XMLHttpRequest.prototype.open = function(...args) {
            // 处理缓存参数
            if (args[1]?.includes('history_messages?chat_session_id')) {
                args[1] = args[1].split('&cache_version=')[0];
            } else if (args[1]?.includes('conversation/info?conversation_id')) {
                args[1] = args[1].split('&cache_version=')[0];
            } else if (args[1]?.includes('/api/user/agent/conversation/v1/detail')) {
                args[1] = args[1].split('&cacheBust=')[0];
            }

            this._requestURL = args[1];
            const url = args[1];
            // 捕获豆包IM请求的完整query串，供批量导出重放接口时使用
            if (location.hostname.includes('doubao') &&
                typeof url === 'string' && url.includes('/im/') && url.includes('?')) {
                state.doubaoApiQuery = url.slice(url.indexOf('?'));
            }
            // 捕获千问接口的query串模板，供批量导出重放接口时使用
            if (location.hostname.includes('qianwen') && typeof url === 'string') {
                if (url.includes('/api/v2/session/changes') && url.includes('?')) {
                    state.qianwenListQuery = url.slice(url.indexOf('?'));
                } else if (url.includes('/api/v1/session/msg/list') && url.includes('?')) {
                    state.qianwenMsgQuery = url.slice(url.indexOf('?'));
                }
            }
            // 捕获元宝消息接口的query串（页面滚动加载时可能带分页参数，学习为模板）
            if (location.hostname.includes('yuanbao') && !state.yuanbaoBatchBusy &&
                typeof url === 'string' && url.includes('/api/user/agent/conversation/v1/detail') &&
                url.includes('?')) {
                state.yuanbaoDetailQuery = url.slice(url.indexOf('?'));
            }
            //console.log('XHR.open被调用:', url);

            this.addEventListener('load', () => {
                // 元宝会话列表请求完成：保存捕获到的headers+body作为批量导出重放模板
                if (this._ybHeaders && this._ybBody !== undefined && !state.yuanbaoBatchBusy) {
                    state.yuanbaoListReq = { headers: this._ybHeaders, body: this._ybBody };
                    logger.info('已捕获元宝会话列表请求模板');
                }
                if (this.responseURL) {
                    //console.log('XHR请求完成:', this.responseURL); // 添加调试
                    for (const [platform, pattern] of Object.entries(PLATFORM_PATTERNS)) {
                        if (pattern.test(this.responseURL)) {
                            processTargetResponse(this.responseText, this.responseURL);
                            break;
                        }
                    }
                }
            });

            originalOpen.apply(this, args);
        };

        // 捕获千问请求的鉴权头（X-DeviceId / X-XSRF-TOKEN / x-user-id），
        // 供批量导出重放接口时使用（签名头 clt-acs-* 经实测可省略）
        const originalSetRequestHeader = pageWindow.XMLHttpRequest.prototype.setRequestHeader;
        pageWindow.XMLHttpRequest.prototype.setRequestHeader = function(header, value) {
            if (location.hostname.includes('qianwen') &&
                typeof this._requestURL === 'string' && this._requestURL.includes('chat2-api.qianwen.com')) {
                captureQianwenAuthHeader(header, value);
            }
            // 捕获DeepSeek鉴权头（Authorization: Bearer + x-device-id）
            if (location.hostname.includes('deepseek') &&
                typeof this._requestURL === 'string' && this._requestURL.includes('/api/v0/')) {
                captureDeepseekAuthHeader(header, value);
            }
            // 累积元宝会话列表请求的全部请求头（请求完成后与body一起存为重放模板）
            if (location.hostname.includes('yuanbao') && !state.yuanbaoBatchBusy &&
                typeof this._requestURL === 'string' &&
                this._requestURL.includes('/api/user/agent/conversation/v3/list')) {
                this._ybHeaders = this._ybHeaders || {};
                this._ybHeaders[header] = value;
            }
            return originalSetRequestHeader.call(this, header, value);
        };

        // 捕获元宝会话列表请求的body（请求完成后与headers一起存为重放模板）
        const originalSend = pageWindow.XMLHttpRequest.prototype.send;
        pageWindow.XMLHttpRequest.prototype.send = function(body) {
            if (location.hostname.includes('yuanbao') && !state.yuanbaoBatchBusy &&
                typeof this._requestURL === 'string' &&
                this._requestURL.includes('/api/user/agent/conversation/v3/list')) {
                this._ybBody = body;
            }
            return originalSend.apply(this, arguments);
        };

        // 拦截Fetch请求
        const originalFetch = pageWindow.fetch;
        pageWindow.fetch = async function(...args) {
            const url = args[0] instanceof Request ? args[0].url : args[0];
            // 捕获豆包IM请求的完整query串，供批量导出重放接口时使用
            if (location.hostname.includes('doubao') &&
                typeof url === 'string' && url.includes('/im/') && url.includes('?')) {
                state.doubaoApiQuery = url.slice(url.indexOf('?'));
            }
            // 捕获千问接口的query串模板与鉴权头，供批量导出重放接口时使用
            if (location.hostname.includes('qianwen') &&
                typeof url === 'string' && url.includes('chat2-api.qianwen.com')) {
                if (url.includes('/api/v2/session/changes') && url.includes('?')) {
                    state.qianwenListQuery = url.slice(url.indexOf('?'));
                } else if (url.includes('/api/v1/session/msg/list') && url.includes('?')) {
                    state.qianwenMsgQuery = url.slice(url.indexOf('?'));
                }
                // 提取鉴权头（兼容 Request 对象、Headers 实例、普通对象三种形式）
                let hdrs = null;
                if (args[0] && args[0].headers) hdrs = args[0].headers;
                else if (args[1] && args[1].headers) hdrs = args[1].headers;
                if (hdrs) {
                    if (typeof hdrs.forEach === 'function') {
                        hdrs.forEach((value, key) => captureQianwenAuthHeader(key, value));
                    } else if (typeof hdrs === 'object') {
                        Object.keys(hdrs).forEach(key => captureQianwenAuthHeader(key, hdrs[key]));
                    }
                }
            }
            // 捕获DeepSeek鉴权头（Authorization: Bearer + x-device-id）
            if (location.hostname.includes('deepseek') &&
                typeof url === 'string' && url.includes('/api/v0/')) {
                let hdrs = null;
                if (args[0] && args[0].headers) hdrs = args[0].headers;
                else if (args[1] && args[1].headers) hdrs = args[1].headers;
                if (hdrs) {
                    if (typeof hdrs.forEach === 'function') {
                        hdrs.forEach((value, key) => captureDeepseekAuthHeader(key, value));
                    } else if (typeof hdrs === 'object') {
                        Object.keys(hdrs).forEach(key => captureDeepseekAuthHeader(key, hdrs[key]));
                    }
                }
            }
            // 捕获元宝会话列表请求模板（headers+body）与消息接口query模板
            if (location.hostname.includes('yuanbao') && !state.yuanbaoBatchBusy && typeof url === 'string') {
                if (url.includes('/api/user/agent/conversation/v3/list')) {
                    let hdrs = null;
                    let body = null;
                    if (args[0] && args[0].headers) {
                        hdrs = args[0].headers;
                        body = args[0].body;
                    } else if (args[1]) {
                        hdrs = args[1].headers;
                        body = args[1].body;
                    }
                    if (hdrs) {
                        const plain = {};
                        if (typeof hdrs.forEach === 'function') hdrs.forEach((value, key) => { plain[key] = value; });
                        else Object.assign(plain, hdrs);
                        state.yuanbaoListReq = { headers: plain, body: body ?? null };
                        logger.info('已捕获元宝会话列表请求模板(fetch)');
                    }
                } else if (url.includes('/api/user/agent/conversation/v1/detail') && url.includes('?')) {
                    state.yuanbaoDetailQuery = url.slice(url.indexOf('?'));
                }
            }
            let response;
            //console.log('🔍 Fetch请求被调用:', url);
            //console.log('⏰ 时间线调试 - 脚本启动:', performance.now());

            try {
                response = await originalFetch.apply(this, args);

                if (typeof url === 'string') {
                    for (const [platform, pattern] of Object.entries(PLATFORM_PATTERNS)) {
                        if (pattern.test(url)) {
                            const contentType = response.headers.get('content-type');
                            if (contentType?.includes('application/json')) {
                                const clonedResponse = response.clone();
                                clonedResponse.text().then(text => {
                                    processTargetResponse(text, url);
                                }).catch(e => {
                                    logger.error(`解析fetch响应文本时出错`, { url, error: e });
                                });
                            }
                            break;
                        }
                    }
                }
            } catch (error) {
                logger.error('Fetch请求失败', error);
                throw error;
            }

            return response;
        };

        // 启动自检：确认钩子确实挂到了页面环境（延迟3秒，避开页面自身初始化）
        setTimeout(() => {
            try {
                const hooked = String(pageWindow.fetch).indexOf('native code') === -1;
                if (hooked) {
                    logger.info('网络拦截钩子已挂载到页面环境，等待捕获对话数据...');
                } else {
                    logger.warn('⚠️ 网络钩子未能挂载到页面环境（页面fetch仍为原生实现），导出功能将无法捕获数据。' +
                        '可尝试：1) 篡改猴 设置→安全 中检查沙箱模式；2) chrome://extensions 开启"开发者模式"后重试。');
                }
            } catch (e) { /* 自检失败不影响主流程 */ }
        }, 3000);
    }

    setupNetworkInterception();
    setupAuthInterceptor(); // Kimi标题授权拦截器

    // 初始化
    function initialize() {
       // setupNetworkInterception();
        ui.createDownloadButtons();
        logger.info(`增强版导出脚本已启动 v${SCRIPT_VERSION}`);

        // 使用MutationObserver确保按钮存在
        const observer = new MutationObserver((mutations) => {
            if (!document.getElementById('downloadJsonButton') ||
                !document.getElementById('downloadMdButton')) {
                logger.info('检测到按钮丢失，正在重新创建...');
                ui.createDownloadButtons();
            }
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    // 页面加载完成后初始化
    if (document.readyState === 'complete') {
        initialize();
    } else {
        window.addEventListener('load', initialize);
    }
})();