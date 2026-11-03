import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ContextHome from '../pages/index.vue'

const api = {
  getContextSources: vi.fn(),
  getContextSettings: vi.fn(),
  updateContextSettings: vi.fn(),
  triggerWechatSync: vi.fn(),
  getBrowserHistoryStatus: vi.fn(),
  prepareBrowserExtension: vi.fn(),
  openBrowserExtensionPage: vi.fn(),
  openGptPlatform: vi.fn(),
  getBrowserConversations: vi.fn(),
  getBrowserConversation: vi.fn(),
  downloadBrowserExport: vi.fn(),
  getBrowserRemoteConfig: vi.fn(),
  saveBrowserRemoteConfig: vi.fn(),
  listBrowserRemoteDevices: vi.fn(),
  pullBrowserRemoteConversations: vi.fn()
}
let wrapper
let navigateToMock

const sourcesPayload = () => ({
  status: 'ok',
  sources: [
    { source_id: 'wechat', name: '微信数据', enabled: true, state: 'up_to_date', last_saved_at: '2026-10-14T10:00:00', summary: { accounts: 1, account: 'wxid_a', successful_databases: 12 }, planned: false, data_dir: '', data_dir_effective: 'C:\\Users\\dev\\AppData\\Roaming\\ContextBuddy\\output\\databases', archive_content: 'structured' },
    { source_id: 'browser_history', name: '浏览器AI问答记录', enabled: true, state: 'idle', last_saved_at: null, planned: false, summary: { platforms: [], total_conversations: 0, total_messages: 0, last_uploaded_at: null, extension: { installed: false, installations: [] } } },
    { source_id: 'computer_activity', name: '电脑操作记录', enabled: false, state: 'planned', last_saved_at: null, summary: { description: '记录本机操作行为' }, planned: true },
    { source_id: 'mobile_upload', name: '手机上传', enabled: false, state: 'planned', last_saved_at: null, summary: { description: '配合手机端 NextCloud 应用' }, planned: true },
    { source_id: 'custom_capture', name: '定制浏览器抓取', enabled: false, state: 'planned', last_saved_at: null, summary: { description: '自动生成定制浏览器插件' }, planned: true }
  ]
})

beforeEach(() => {
  navigateToMock = vi.fn()
  vi.stubGlobal('useApi', () => api)
  vi.stubGlobal('navigateTo', navigateToMock)
  api.getContextSources.mockReset().mockImplementation(async () => sourcesPayload())
  api.getContextSettings.mockReset().mockResolvedValue({ status: 'ok', settings: { wechat: { enabled: true }, browser_history: { enabled: true } } })
  api.updateContextSettings.mockReset().mockResolvedValue({ status: 'ok', settings: { wechat: { enabled: false }, browser_history: { enabled: true } } })
  api.triggerWechatSync.mockReset().mockResolvedValue({ status: 0, errmsg: 'ok', state: 'up_to_date', summary: { accounts: 1 } })
  api.getBrowserHistoryStatus.mockReset().mockResolvedValue({ status: 'ok', installed: false, installations: [], extension_name: 'AI对话导出 word/json/md' })
  api.prepareBrowserExtension.mockReset().mockResolvedValue({ status: 'ok', extension_dir: 'C:\\data\\browser_history\\extension', firefox_extension_dir: 'C:\\data\\browser_history\\extension\\firefox', upload_url: 'http://127.0.0.1:10392' })
  api.openBrowserExtensionPage.mockReset().mockResolvedValue({ status: 'ok' })
  api.openGptPlatform.mockReset().mockResolvedValue({ status: 'ok' })
  api.getBrowserConversations.mockReset().mockResolvedValue({ total: 0, conversations: [] })
  api.getBrowserConversation.mockReset().mockResolvedValue({ id: 0, messages: [] })
  api.downloadBrowserExport.mockReset().mockResolvedValue('export.md')
  api.getBrowserRemoteConfig.mockReset().mockResolvedValue({ server_url: '', token: '', device_id: '', local_device_id: '' })
  api.saveBrowserRemoteConfig.mockReset().mockResolvedValue({ server_url: '', token: '', device_id: '', local_device_id: '' })
  api.listBrowserRemoteDevices.mockReset().mockResolvedValue({ devices: [] })
  api.pullBrowserRemoteConversations.mockReset().mockResolvedValue({ server_total: 0, imported: 0, skipped: 0, failed: 0 })
})

afterEach(() => { wrapper?.unmount(); vi.unstubAllGlobals() })

const mountHome = async () => {
  wrapper = mount(ContextHome, { global: { stubs: { GlobalExportDialog: true, WechatDetectionPanel: true } } })
  await flushPromises()
  return wrapper
}

describe('上下文中心首页', () => {
  it('渲染微信卡片与三个规划中的数据源', async () => {
    const w = await mountHome()
    expect(w.text()).toContain('微信数据')
    expect(w.text()).toContain('已是最新')
    expect(w.text()).toContain('2026-10-14T10:00:00')
    const plannedBadges = w.findAll('.rounded-full').filter(b => b.text() === '规划中')
    expect(plannedBadges).toHaveLength(3)
  })

  it('点击「浏览器AI问答记录」后主界面切换为浏览器AI问答记录信息', async () => {
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    // 微信卡片区域被替换为浏览器AI问答记录卡片
    expect(w.text()).toContain('浏览器AI问答记录')
    expect(w.text()).toContain('扩展未安装')
    expect(w.text()).toContain('检查是否已安装')
    expect(w.text()).toContain('安装插件')
    expect(w.text()).toContain('GPT 平台')
    expect(w.text()).toContain('DeepSeek')
    expect(w.text()).toContain('豆包')
    expect(w.text()).not.toContain('立即保存')
    expect(api.getBrowserHistoryStatus).toHaveBeenCalled()
  })

  it('安装插件生成副本并展示安装步骤说明', async () => {
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '安装插件').trigger('click')
    await flushPromises()
    expect(api.prepareBrowserExtension).toHaveBeenCalledTimes(1)
    expect(api.openBrowserExtensionPage).toHaveBeenCalledWith('chrome')
    expect(w.text()).toContain('加载已解压的扩展程序')
    expect(w.text()).toContain('C:\\data\\browser_history\\extension')
  })

  it('浏览器未能自动打开时展示手动打开扩展管理页的引导', async () => {
    api.openBrowserExtensionPage.mockReset().mockResolvedValue({ status: 'ok', ok: false, error: '未找到 Chrome/Edge 浏览器' })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '安装插件').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('未能自动打开浏览器')
    expect(w.text()).toContain('chrome://extensions/')
    expect(w.text()).toContain('加载已解压的扩展程序')
  })

  it('Chrome 已在运行时不重复启动并给出提示', async () => {
    api.openBrowserExtensionPage.mockReset().mockResolvedValue({ status: 'ok', ok: true, already_running: true })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '安装插件').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('检测到 Chrome / Edge 已在运行，未重复启动')
    expect(w.text()).toContain('chrome://extensions/')
    expect(w.text()).toContain('加载已解压的扩展程序')
  })

  it('选择 Firefox 时按 Firefox 引导安装并打开调试页', async () => {
    api.openBrowserExtensionPage.mockReset().mockResolvedValue({ status: 'ok', ok: false, error: '未找到 Firefox 浏览器' })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.find('select').setValue('firefox')
    await w.findAll('button').find(b => b.text() === '安装插件').trigger('click')
    await flushPromises()
    expect(api.openBrowserExtensionPage).toHaveBeenCalledWith('firefox')
    expect(w.text()).toContain('未能自动打开浏览器')
    expect(w.text()).toContain('about:debugging#/runtime/this-firefox')
    expect(w.text()).toContain('临时载入附加组件')
    expect(w.text()).toContain('C:\\data\\browser_history\\extension\\firefox')
  })

  it('复制路径后安装步骤说明仍保持显示', async () => {
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockResolvedValue() } })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '安装插件').trigger('click')
    await flushPromises()
    const steps = '插件副本已生成（上传地址已指向本机），已打开 Chrome / Edge 扩展管理页，请按以下步骤完成安装：'
    expect(w.text()).toContain(steps)
    await w.findAll('button').find(b => b.text() === '复制路径').trigger('click')
    await flushPromises()
    expect(w.text()).toContain(steps)
    expect(w.text()).toContain('加载已解压的扩展程序')
    expect(w.text()).toContain('已复制')
  })

  it('点击平台卡片打开对应 GPT 平台网站', async () => {
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    const card = w.findAll('.cursor-pointer').find(el => el.text().includes('DeepSeek'))
    await card.trigger('click')
    await flushPromises()
    expect(api.openGptPlatform).toHaveBeenCalledWith('https://chat.deepseek.com/')
  })

  it('切换到浏览器AI问答记录后加载并展示已同步会话列表', async () => {
    api.getBrowserConversations.mockReset().mockResolvedValue({
      total: 2,
      conversations: [
        { id: 1, platform_code: 'deepseek', platform_name: 'DeepSeek', title: '第一个会话', message_count: 4, char_count: 100, received_at: '2026-10-20T10:00:00' },
        { id: 2, platform_code: 'kimi', platform_name: 'Kimi', title: '第二个会话', message_count: 6, char_count: 200, received_at: '2026-10-21T09:00:00' }
      ]
    })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    expect(api.getBrowserConversations).toHaveBeenCalled()
    expect(w.text()).toContain('已同步会话')
    expect(w.text()).toContain('第一个会话')
    expect(w.text()).toContain('第二个会话')
    expect(w.text()).toContain('下载全部（zip）')
    expect(w.text()).toContain('下载 .md')
  })

  it('点击会话行展开显示消息明细，再点收起', async () => {
    api.getBrowserConversations.mockReset().mockResolvedValue({
      total: 1,
      conversations: [{ id: 7, platform_code: 'deepseek', platform_name: 'DeepSeek', title: '展开会话', message_count: 2, char_count: 10, received_at: '2026-10-21T09:00:00' }]
    })
    api.getBrowserConversation.mockReset().mockResolvedValue({
      id: 7,
      title: '展开会话',
      messages: [
        { role: 'user', content: '用户的问题', msg_time: '2026-10-20T10:00:00', thinking_content: null },
        { role: 'assistant', content: 'AI 的回答', msg_time: '2026-10-20T10:00:05', thinking_content: '推理过程' }
      ]
    })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    const row = w.findAll('div.cursor-pointer').find(el => el.text().includes('展开会话'))
    await row.trigger('click')
    await flushPromises()
    expect(api.getBrowserConversation).toHaveBeenCalledWith(7)
    expect(w.text()).toContain('用户的问题')
    expect(w.text()).toContain('AI 的回答')
    expect(w.text()).toContain('思考过程')
    await row.trigger('click')
    await flushPromises()
    expect(w.text()).not.toContain('AI 的回答')
  })

  it('下载单会话与下载全部分别调用导出接口并提示文件名', async () => {
    api.getBrowserConversations.mockReset().mockResolvedValue({
      total: 1,
      conversations: [{ id: 9, platform_code: 'kimi', platform_name: 'Kimi', title: '下载会话', message_count: 2, char_count: 10, received_at: '2026-10-21T09:00:00' }]
    })
    api.downloadBrowserExport.mockReset().mockResolvedValue('Kimi-下载会话-9.md')
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '下载 .md').trigger('click')
    await flushPromises()
    expect(api.downloadBrowserExport).toHaveBeenCalledWith({ ids: '9' })
    expect(w.text()).toContain('已下载：Kimi-下载会话-9.md')
    await w.findAll('button').find(b => b.text() === '下载全部（zip）').trigger('click')
    await flushPromises()
    expect(api.downloadBrowserExport).toHaveBeenCalledWith({})
  })

  it('平台筛选切换后按平台重新加载会话列表', async () => {
    api.getContextSources.mockReset().mockImplementation(async () => {
      const payload = sourcesPayload()
      payload.sources[1].summary.platforms = [{ code: 'kimi', name: 'Kimi', conversations: 2, messages: 8 }]
      return payload
    })
    api.getBrowserConversations.mockReset().mockResolvedValue({ total: 0, conversations: [] })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    const kimiBtn = w.findAll('button').find(b => b.text() === 'Kimi')
    expect(kimiBtn).toBeDefined()
    await kimiBtn.trigger('click')
    await flushPromises()
    expect(api.getBrowserConversations).toHaveBeenLastCalledWith(expect.objectContaining({ platform: 'kimi' }))
  })

  it('切换到浏览器AI问答记录后加载服务端拉取配置并展示区域', async () => {
    api.getBrowserRemoteConfig.mockReset().mockResolvedValue({
      server_url: 'http://192.168.1.5:8321',
      token: 'srv-token',
      device_id: '',
      local_device_id: 'dev-my-laptop'
    })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    expect(api.getBrowserRemoteConfig).toHaveBeenCalled()
    expect(w.text()).toContain('从服务端拉取')
    expect(w.text()).toContain('dev-my-laptop')
    expect(w.find('input[placeholder="例如 http://192.168.1.5:8321"]').element.value).toBe('http://192.168.1.5:8321')
  })

  it('点击从服务端拉取调用接口并刷新会话列表', async () => {
    api.pullBrowserRemoteConversations.mockReset().mockResolvedValue({ server_total: 5, imported: 3, skipped: 2, failed: 0 })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '从服务端拉取').trigger('click')
    await flushPromises()
    expect(api.pullBrowserRemoteConversations).toHaveBeenCalled()
    expect(w.text()).toContain('拉取完成：服务端 5 个会话，导入 3 个（跳过 2，失败 0）')
    // 拉取后刷新本地会话列表
    expect(api.getBrowserConversations.mock.calls.filter(c => (c[0] || {}).offset === 0).length).toBeGreaterThanOrEqual(2)
  })

  it('查看服务端设备展示设备列表并可选中', async () => {
    api.listBrowserRemoteDevices.mockReset().mockResolvedValue({
      devices: [
        { device_id: 'dev-a', conversations: 3, messages: 10, last_active: '2026-01-01' },
        { device_id: 'dev-b', conversations: 1, messages: 4, last_active: '2026-01-02' }
      ]
    })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '浏览器AI问答记录').trigger('click')
    await flushPromises()
    await w.findAll('button').find(b => b.text() === '查看服务端设备').trigger('click')
    await flushPromises()
    expect(api.listBrowserRemoteDevices).toHaveBeenCalled()
    expect(w.text()).toContain('dev-a')
    expect(w.text()).toContain('dev-b')
    expect(w.text()).toContain('3 个会话 / 10 条消息')
  })

  it('切换自动保存开关调用设置接口', async () => {
    const w = await mountHome()
    await w.find('input[type=checkbox]').setValue(false)
    await flushPromises()
    expect(api.updateContextSettings).toHaveBeenCalled()
    expect(api.updateContextSettings.mock.calls[0][0]).toEqual({ wechat: { enabled: false } })
  })

  it('立即保存调用触发接口并展示结果', async () => {
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '立即保存').trigger('click')
    await flushPromises()
    expect(api.triggerWechatSync).toHaveBeenCalledTimes(1)
    expect(w.text()).toContain('本轮保存已完成。')
  })

  it('归档内容默认结构化的数据，切换调用设置接口', async () => {
    const w = await mountHome()
    const select = w.find('select')
    expect(select.element.value).toBe('structured')
    expect(w.text()).toContain('归档内容')
    expect(w.text()).toContain('JSON 与 TXT')
    await select.setValue('databases')
    await flushPromises()
    expect(api.updateContextSettings).toHaveBeenCalledWith({ wechat: { archive_content: 'databases' } })
    expect(w.text()).toContain('归档内容已切换为数据库文件')
  })

  it('归档内容为结构化的数据时，立即保存完成后提示结构化数据已更新', async () => {
    api.triggerWechatSync.mockReset().mockResolvedValue({ status: 0, errmsg: 'ok', state: 'up_to_date', archive_content: 'structured', structured: { ok: true, conversations: 3, messages: 120 } })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '立即保存').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('结构化的数据已更新')
  })

  it('结构化数据更新失败时保存仍算完成，提示下轮自动重试', async () => {
    api.triggerWechatSync.mockReset().mockResolvedValue({ status: 0, errmsg: 'ok', state: 'up_to_date', archive_content: 'structured', structured: { ok: false } })
    const w = await mountHome()
    await w.findAll('button').find(b => b.text() === '立即保存').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('本轮保存已完成')
    expect(w.text()).toContain('下轮保存会自动重试')
  })

  it('显示保存位置的生效路径，默认不显示恢复默认按钮', async () => {
    const w = await mountHome()
    expect(w.text()).toContain('保存位置')
    expect(w.text()).toContain('C:\\Users\\dev\\AppData\\Roaming\\ContextBuddy\\output\\databases')
    expect(w.findAll('button').find(b => b.text() === '恢复默认')).toBeUndefined()
  })

  it('更改保存位置调用设置接口并提示完成', async () => {
    const w = await mountHome()
    const promptMock = vi.spyOn(window, 'prompt').mockReturnValue('D:\\MyWechatData')
    try {
      await w.findAll('button').find(b => b.text() === '更改').trigger('click')
      await flushPromises()
      expect(promptMock).toHaveBeenCalled()
      expect(api.updateContextSettings).toHaveBeenCalledWith({ wechat: { data_dir: 'D:\\MyWechatData' } })
      expect(w.text()).toContain('保存位置已更新')
    } finally {
      promptMock.mockRestore()
    }
  })

  it('自定义路径时显示恢复默认，点击后传空路径', async () => {
    api.getContextSources.mockReset().mockImplementation(async () => {
      const payload = sourcesPayload()
      payload.sources[0].data_dir = 'D:\\MyWechatData'
      payload.sources[0].data_dir_effective = 'D:\\MyWechatData'
      return payload
    })
    const w = await mountHome()
    const resetButton = w.findAll('button').find(b => b.text() === '恢复默认')
    expect(resetButton).toBeDefined()
    await resetButton.trigger('click')
    await flushPromises()
    expect(api.updateContextSettings).toHaveBeenCalledWith({ wechat: { data_dir: '' } })
    expect(w.text()).toContain('已恢复默认保存位置。')
  })

  it('页面文案不出现解密与密钥字样', async () => {
    const w = await mountHome()
    expect(w.text()).not.toContain('解密')
    expect(w.text()).not.toContain('密钥')
  })

  it('账号检测以浮层打开，主界面保留且不发生导航', async () => {
    const w = await mountHome()
    await w.findAll('a').find(a => a.text() === '账号检测').trigger('click')
    await flushPromises()
    expect(navigateToMock).not.toHaveBeenCalled()
    // 主界面（微信卡片）仍在 DOM 中
    expect(w.text()).toContain('微信数据')
    // 浮层容器与检测面板出现
    expect(w.find('.wechat-detection-overlay-card').exists()).toBe(true)
    expect(w.findComponent({ name: 'WechatDetectionPanel' }).exists()).toBe(true)
  })

  it('点击浮层背景关闭账号检测浮层', async () => {
    const w = await mountHome()
    await w.findAll('a').find(a => a.text() === '账号检测').trigger('click')
    await flushPromises()
    await w.find('.wechat-detection-overlay-backdrop').trigger('click')
    await flushPromises()
    expect(w.find('.wechat-detection-overlay-card').exists()).toBe(false)
    expect(w.findComponent({ name: 'WechatDetectionPanel' }).exists()).toBe(false)
  })

  it('查看微信上下文打开独立子窗口，主界面保留且不发生导航', async () => {
    const w = await mountHome()
    const openMock = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      await w.findAll('button').find(b => b.text() === '查看微信上下文').trigger('click')
      await flushPromises()
      expect(openMock).toHaveBeenCalledWith('/chat', '_blank')
      expect(navigateToMock).not.toHaveBeenCalled()
      // 主界面（微信卡片）仍在 DOM 中
      expect(w.text()).toContain('微信数据')
    } finally {
      openMock.mockRestore()
    }
  })
})
