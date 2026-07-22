import OpenAI from 'openai'
import type { ReviewerConfig } from '../config.js'

export default async function review(
  plan: string,
  prompt: string,
  config: ReviewerConfig,
  signal: AbortSignal,
): Promise<string> {
  const apiKey = process.env[config.apiKeyEnv]
  if (!apiKey) {
    throw new Error(`Missing environment variable: ${config.apiKeyEnv}`)
  }

  const client = new OpenAI({ apiKey })
  const response = await client.chat.completions.create(
    {
      model: config.model,
      max_completion_tokens: config.maxTokens,
      messages: [
        { role: 'system', content: prompt },
        { role: 'user', content: plan },
      ],
    },
    { signal },
  )

  const text = response.choices[0]?.message?.content
  if (!text) {
    throw new Error('Response contained no text content')
  }
  return text
}
