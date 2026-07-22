import { REVIEWERS, type ReviewerConfig } from './config.js'
import { pathToFileURL } from 'url'
import path from 'path'

export interface ReviewResult {
  key: string
  displayName: string
  status: 'success' | 'error' | 'timeout'
  content: string
  durationMs: number
}

type ReviewerFn = (
  plan: string,
  prompt: string,
  config: ReviewerConfig,
  signal: AbortSignal,
) => Promise<string>

async function loadReviewer(config: ReviewerConfig): Promise<ReviewerFn> {
  const modulePath = path.resolve(
    path.dirname(new URL(import.meta.url).pathname),
    config.modulePath,
  )
  const mod = await import(pathToFileURL(modulePath).href)
  return mod.default as ReviewerFn
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`Timed out after ${ms}ms`))
    }, ms)

    promise.then(
      (val) => { clearTimeout(timer); resolve(val) },
      (err) => { clearTimeout(timer); reject(err) },
    )
  })
}

export async function runReviews(
  plan: string,
  prompt: string,
  reviewerKeys: string[],
): Promise<ReviewResult[]> {
  for (const key of reviewerKeys) {
    if (!REVIEWERS[key]) {
      throw new Error(
        `Unknown reviewer: "${key}". Available: ${Object.keys(REVIEWERS).join(', ')}`,
      )
    }
  }

  const tasks = reviewerKeys.map(async (key): Promise<ReviewResult> => {
    const config = REVIEWERS[key]
    const start = Date.now()

    try {
      const reviewerFn = await loadReviewer(config)
      const controller = new AbortController()
      const result = await withTimeout(
        reviewerFn(plan, prompt, config, controller.signal),
        config.timeoutMs,
      )
      controller.abort()

      return {
        key,
        displayName: config.displayName,
        status: 'success',
        content: result,
        durationMs: Date.now() - start,
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      const isTimeout = message.includes('Timed out after')

      return {
        key,
        displayName: config.displayName,
        status: isTimeout ? 'timeout' : 'error',
        content: message,
        durationMs: Date.now() - start,
      }
    }
  })

  return Promise.all(tasks)
}
