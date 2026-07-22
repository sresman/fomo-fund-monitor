import type { ReviewResult } from './review.js'

export function formatResults(results: ReviewResult[]): string {
  return results
    .map((r, i) => {
      const header = `## ${r.displayName} Review`
      const timing = `*${(r.durationMs / 1000).toFixed(1)}s*`

      let body: string
      if (r.status === 'success') {
        body = r.content
      } else if (r.status === 'timeout') {
        body = `**Timed out** (${r.content})`
      } else {
        body = `**Error:** ${r.content}`
      }

      const separator = i < results.length - 1 ? '\n\n---\n' : ''
      return `${header}\n${timing}\n\n${body}${separator}`
    })
    .join('\n')
}
