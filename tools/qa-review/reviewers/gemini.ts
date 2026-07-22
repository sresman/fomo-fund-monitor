import { GoogleGenAI, type ThinkingLevel } from '@google/genai'
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

  const thinkingLevel = config.extra?.thinkingLevel as ThinkingLevel | undefined

  const client = new GoogleGenAI({ apiKey })
  const response = await client.models.generateContent({
    model: config.model,
    contents: plan,
    config: {
      systemInstruction: prompt,
      maxOutputTokens: config.maxTokens,
      ...(thinkingLevel ? { thinkingConfig: { thinkingLevel } } : {}),
      abortSignal: signal,
    },
  })

  const text = response.text
  if (!text) {
    throw new Error('Response contained no text content')
  }
  return text
}
