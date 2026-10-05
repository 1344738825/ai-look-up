/**
 * OpenCode → ai-look-up 适配 shim(实验性)。
 *
 * 放置:.opencode/plugin/ai-look-up-adapter.js(OpenCode 自动加载 plugin 目录)
 * 前提:本机可用 python,且已克隆 ai-look-up 仓库;把 REPO 指向仓库根。
 *
 * 事件映射(核心协议:prompt / post-use / post-fail / stop):
 *   chat.message            → prompt(重置回合计数)
 *   tool.execute.after      → post-use / post-fail(output.error 推导)
 *
 * 已知限制:
 *   - `opencode run` 一次性模式下 tool.execute.after 不触发(OpenCode 官方已知问题),
 *     此 shim 只在交互会话中有效;
 *   - output.error 的字段名以所用 OpenCode 版本为准,如不匹配请调整 detectFail();
 *   - 通过子进程回调 Python 核心,保持"一份核心逻辑"——本文件不含任何检测逻辑。
 */

const REPO = process.env.AI_LOOK_UP_REPO || 'REPLACE_WITH_REPO_PATH';
const CORE = REPO + '/hooks/lookup_hook.py';
const PYTHON = process.env.AI_LOOK_UP_PYTHON || 'python';

let sessionId = 'opencode-' + Date.now().toString(36);

/** 把 OpenCode 的工具事件归一成核心协议 payload。 */
function toCorePayload(input, output) {
  const tool = input?.tool || input?.name || 'Unknown';
  const rawInput = input?.input ?? input?.arguments ?? input?.args ?? {};
  return {
    session_id: sessionId,
    tool_name: tool,
    tool_input: typeof rawInput === 'object' ? rawInput : { command: String(rawInput) },
    _error: detectFail(output),
  };
}

function detectFail(output) {
  if (!output) return false;
  return Boolean(output.error || output.isError || output.is_error);
}

async function callCore(action, payload) {
  const { spawn } = await import('node:child_process');
  return new Promise((resolve) => {
    try {
      const proc = spawn(PYTHON, [CORE, action], { stdio: ['pipe', 'pipe', 'ignore'] });
      let out = '';
      proc.stdout.on('data', (d) => { out += d; });
      proc.on('close', () => resolve(out));
      proc.on('error', () => resolve(''));
      proc.stdin.write(JSON.stringify(payload));
      proc.stdin.end();
    } catch {
      resolve('');
    }
  });
}

export const AiLookUpAdapter = async ({ project }) => {
  // session 标识:用会话目录区分,保证状态文件不串
  sessionId = 'opencode-' + (project || 'default').replace(/[^A-Za-z0-9_.-]/g, '_').slice(-80);
  return {
    'tool.execute.after': async (input, output) => {
      const payload = toCorePayload(input, output);
      const out = await callCore(payload._error ? 'post-fail' : 'post-use', payload);
      if (out) {
        // 核心输出 hookSpecificOutput JSON;OpenCode 插件 API 没有 additionalContext
        // 通道,打印到 stderr 供调试,或按你的 OpenCode 版本接入对应注入 API。
        console.error('[ai-look-up] ' + out);
      }
      return output;
    },
  };
};
