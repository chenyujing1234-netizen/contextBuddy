<template>
  <div v-if="open" class="fixed inset-0 z-[200] flex items-center justify-center p-4">
    <div class="absolute inset-0 bg-[rgba(14,22,18,0.44)]"></div>
    <div class="relative w-full max-w-sm rounded-2xl border border-[#e8e8e8] bg-white p-6 shadow-[0_24px_80px_-24px_rgba(0,0,0,0.45)]">
      <button
        type="button"
        aria-label="关闭"
        class="absolute right-3 top-3 flex h-8 w-8 items-center justify-center rounded-full text-[#8a8a8a] transition hover:bg-[#f2f2f2] hover:text-[#333]"
        @click="emit('close')"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
        </svg>
      </button>
      <h2 class="text-center text-xl font-semibold text-[#000000e6]">手机号登录</h2>
      <p class="mt-2 text-center text-[13px] leading-5 text-[#7F7F7F]">登录后才能使用全部功能</p>

      <div class="mt-6">
        <label class="block text-[13px] text-[#5f5f5f]">手机号</label>
        <input
          v-model="phone"
          type="tel"
          maxlength="11"
          placeholder="请输入手机号"
          class="mt-1 w-full rounded-[8px] border border-[#e4e4e4] px-3 py-2 text-[14px] outline-none transition focus:border-[#07c160]"
        />
      </div>

      <div class="mt-4">
        <label class="block text-[13px] text-[#5f5f5f]">验证码</label>
        <div class="mt-1 flex gap-2">
          <input
            v-model="code"
            type="text"
            maxlength="4"
            placeholder="4位验证码"
            class="flex-1 rounded-[8px] border border-[#e4e4e4] px-3 py-2 text-[14px] outline-none transition focus:border-[#07c160]"
          />
          <button
            type="button"
            :disabled="countdown > 0 || sending"
            class="shrink-0 rounded-[8px] bg-[#07c160] px-3 py-2 text-[13px] text-white transition hover:bg-[#06ad56] disabled:cursor-not-allowed disabled:opacity-50"
            @click="sendCode"
          >
            {{ countdown > 0 ? `${countdown}s 后重发` : sending ? '发送中…' : '获取验证码' }}
          </button>
        </div>
      </div>

      <p v-if="message" class="mt-3 text-[12px]" :class="messageOk ? 'text-[#07964c]' : 'text-red-500'">
        {{ message }}
      </p>

      <button
        type="button"
        :disabled="submitting"
        class="mt-5 w-full rounded-[8px] bg-[#07c160] py-2.5 text-[14px] font-medium text-white transition hover:bg-[#06ad56] disabled:cursor-not-allowed disabled:opacity-60"
        @click="login"
      >
        {{ submitting ? '登录中…' : '登录' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { onUnmounted, ref } from 'vue'

defineProps({
  open: { type: Boolean, default: false }
})
const emit = defineEmits(['success', 'close'])

const { sendAuthCode, loginWithCode } = useApi()

const phone = ref('')
const code = ref('')
const message = ref('')
const messageOk = ref(true)
const sending = ref(false)
const submitting = ref(false)
const countdown = ref(0)
let timer = null

const clearTimer = () => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

const sendCode = async () => {
  const p = phone.value.trim()
  if (!/^1[3-9]\d{9}$/.test(p)) {
    message.value = '请输入有效的手机号'
    messageOk.value = false
    return
  }
  sending.value = true
  message.value = ''
  try {
    const resp = await sendAuthCode(p)
    if (resp?.debug_code) {
      // 本地联调（后端 WECHAT_TOOL_AUTH_DEBUG=1）：直接把验证码填进输入框
      code.value = String(resp.debug_code)
      message.value = `验证码已发送（本地调试：${resp.debug_code}）`
    } else {
      message.value = resp?.message || '验证码已发送，请注意查收'
    }
    messageOk.value = true
    countdown.value = 60
    clearTimer()
    timer = setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0) clearTimer()
    }, 1000)
  } catch (e) {
    message.value = e?.message || '发送验证码失败'
    messageOk.value = false
  } finally {
    sending.value = false
  }
}

const login = async () => {
  const p = phone.value.trim()
  const c = code.value.trim()
  if (!/^1[3-9]\d{9}$/.test(p)) {
    message.value = '请输入有效的手机号'
    messageOk.value = false
    return
  }
  if (!/^\d{4}$/.test(c)) {
    message.value = '请输入4位数字验证码'
    messageOk.value = false
    return
  }
  submitting.value = true
  message.value = ''
  try {
    const resp = await loginWithCode(p, c)
    if (resp?.logged_in) {
      emit('success', p)
    }
  } catch (e) {
    message.value = e?.message || '登录失败'
    messageOk.value = false
  } finally {
    submitting.value = false
  }
}

onUnmounted(clearTimer)
</script>