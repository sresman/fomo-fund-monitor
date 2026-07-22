import { readFileSync, writeFileSync, existsSync } from 'fs'
import { runReviews } from './review.js'
import { formatResults } from './format.js'
import { DEFAULT_REVIEWER_KEYS, DEFAULT_PROMPT } from './config.js'

interface CliArgs {
  file: string | null
  prompt: string
  reviewers: string[]
  out: string | null
}

function parseArgs(argv: string[]): CliArgs {
  const args = argv.slice(2)
  let file: string | null = null
  let prompt = DEFAULT_PROMPT
  let reviewers = DEFAULT_REVIEWER_KEYS
  let out: string | null = null

  for (let i = 0; i < args.length; i++) {
    const arg = args[i]
    if (arg === '--prompt') {
      prompt = args[++i] ?? prompt
    } else if (arg === '--reviewers') {
      reviewers = (args[++i] ?? '').split(',').map((s) => s.trim()).filter(Boolean)
    } else if (arg === '--out') {
      out = args[++i] ?? null
    } else if (arg === '--help' || arg === '-h') {
      printUsage()
      process.exit(0)
    } else if (!arg.startsWith('--')) {
      file = arg
    }
  }

  return { file, prompt, reviewers, out }
}

function printUsage(): void {
  console.log(`Usage: npx tsx tools/qa-review/cli.ts [file] [options]

Options:
  --prompt "..."         Custom review prompt (default: "${DEFAULT_PROMPT}")
  --reviewers a,b,c      Comma-separated reviewer keys (default: all)
  --out path.md          Write output to file instead of stdout
  -h, --help             Show this help

Examples:
  npx tsx tools/qa-review/cli.ts plan.md
  cat plan.md | npx tsx tools/qa-review/cli.ts
  npx tsx tools/qa-review/cli.ts plan.md --reviewers claude,openai
  npx tsx tools/qa-review/cli.ts plan.md --prompt "focus on error handling"
  npx tsx tools/qa-review/cli.ts plan.md --out reviews.md`)
}

async function readStdin(): Promise<string> {
  if (process.stdin.isTTY) {
    return ''
  }

  const chunks: Buffer[] = []
  for await (const chunk of process.stdin) {
    chunks.push(chunk as Buffer)
  }
  return Buffer.concat(chunks).toString('utf-8')
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv)

  let plan = ''
  if (args.file) {
    if (!existsSync(args.file)) {
      console.error(`Error: File not found: ${args.file}`)
      process.exit(1)
    }
    plan = readFileSync(args.file, 'utf-8')
  } else {
    plan = await readStdin()
  }

  if (!plan.trim()) {
    console.error('Error: No plan provided. Pass a file path or pipe via stdin.')
    printUsage()
    process.exit(1)
  }

  const reviewerList = args.reviewers.join(', ')
  console.error(`Sending to reviewers: ${reviewerList}`)
  console.error(`Plan length: ${plan.length} characters\n`)

  const results = await runReviews(plan, args.prompt, args.reviewers)
  const output = formatResults(results)

  if (args.out) {
    writeFileSync(args.out, output, 'utf-8')
    console.error(`\nWritten to ${args.out}`)
  } else {
    console.log(output)
  }

  const allFailed = results.every((r) => r.status !== 'success')
  if (allFailed) {
    process.exit(1)
  }
}

main().catch((err) => {
  console.error(`Fatal: ${err.message}`)
  process.exit(1)
})
