import Anthropic from '@anthropic-ai/sdk'
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

  const client = new Anthropic({ apiKey })
  const response = await client.messages.create(
    {
      model: config.model,
      max_tokens: config.maxTokens,
      system: prompt,
      messages: [{ role: 'user', content: plan }],
    },
    { signal },
  )

  const block = response.content[0]
  if (!block || block.type !== 'text') {
    throw new Error('Response contained no text content')
  }
  return block.text
}
