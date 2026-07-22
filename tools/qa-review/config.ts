export interface ReviewerConfig {
  key: string
  displayName: string
  model: string
  apiKeyEnv: string
  maxTokens: number
  timeoutMs: number
  modulePath: string
  extra?: Record<string, unknown>
}

export const DEFAULT_PROMPT =
  'You are a code plan reviewer. Your job is to critique this plan for potential issues, gaps, contradictions, risks, and missing edge cases. Do not implement anything. Do not write code. Do not generate bash commands. Provide critical feedback only.'
export const DEFAULT_TIMEOUT_MS = 300_000
export const DEFAULT_MAX_TOKENS = 8192

export const REVIEWERS: Record<string, ReviewerConfig> = {
  claude: {
    key: 'claude',
    displayName: 'Claude (Opus)',
    model: 'claude-opus-4-6',
    apiKeyEnv: 'ANTHROPIC_API_KEY',
    maxTokens: DEFAULT_MAX_TOKENS,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    modulePath: './reviewers/claude.js',
  },
  openai: {
    key: 'openai',
    displayName: 'OpenAI (GPT-5.5)',
    model: 'gpt-5.5',
    apiKeyEnv: 'OPENAI_API_KEY',
    maxTokens: DEFAULT_MAX_TOKENS,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    modulePath: './reviewers/openai.js',
  },
  gemini: {
    key: 'gemini',
    displayName: 'Gemini (3.1 Pro)',
    model: 'gemini-3.1-pro-preview',
    apiKeyEnv: 'GEMINI_API_KEY',
    maxTokens: DEFAULT_MAX_TOKENS,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    modulePath: './reviewers/gemini.js',
    extra: { thinkingLevel: 'MEDIUM' },
  },
}

export const DEFAULT_REVIEWER_KEYS = Object.keys(REVIEWERS)
