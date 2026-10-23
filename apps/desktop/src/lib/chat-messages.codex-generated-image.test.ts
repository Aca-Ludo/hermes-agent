import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { toChatMessages, upsertToolPart } from '@/lib/chat-messages'
import { generatedImageFromResult } from '@/lib/generated-images'
import type { SessionMessage } from '@/types/hermes'

interface ImageReceipt {
  completion: { name: string; tool_id: string; args: Record<string, unknown>; result: string }
  rows: SessionMessage[]
}

const callId = 'codex_image_generate_native-image'
const result = JSON.stringify({ success: true, provider: 'codex', image: '/fixture/generated/native-image.png' })

const fixture: ImageReceipt = {
  completion: { name: 'image_generate', tool_id: callId, args: {}, result },
  rows: [
    { role: 'user', content: 'Generate an image' },
    {
      role: 'assistant',
      content: '',
      tool_calls: [{ id: callId, type: 'function', function: { name: 'image_generate', arguments: '{}' } }]
    },
    { role: 'tool', tool_call_id: callId, content: result },
    { role: 'assistant', content: 'Image ready.' }
  ]
}

// Optional receipt from the actual Python session/bridge/SQLite path, not source text.
const receipt: ImageReceipt = process.env.CODEX_IMAGE_RECEIPT
  ? JSON.parse(readFileSync(process.env.CODEX_IMAGE_RECEIPT, 'utf8'))
  : fixture

describe('native Codex generated-image wire contract', () => {
  it('uses the same single image card during live completion and cold REST hydration', () => {
    const running = upsertToolPart([], { ...receipt.completion, result: undefined }, 'running', 1)
    const live = upsertToolPart(running, receipt.completion, 'complete', 2)
    const replay = upsertToolPart(live, receipt.completion, 'complete', 3)
    const cold = toChatMessages(receipt.rows).flatMap(message => message.parts)
    const liveTools = replay.filter(part => part.type === 'tool-call')
    const coldTools = cold.filter(part => part.type === 'tool-call')

    expect(liveTools).toHaveLength(1)
    expect(coldTools).toHaveLength(1)

    for (const part of [...liveTools, ...coldTools]) {
      expect(part.toolCallId).toBe(receipt.completion.tool_id)
      expect(part.toolName).toBe('image_generate')
      expect(generatedImageFromResult(part.result)).toBe(JSON.parse(receipt.completion.result).image)
    }

    expect(JSON.stringify({ live, cold })).not.toContain('[codex imageGeneration]')
    expect(JSON.stringify({ live, cold })).not.toContain('iVBORw0KGgo')
    expect(cold.some(part => part.type === 'text' && part.text.includes('Image ready.'))).toBe(true)
  })
})
