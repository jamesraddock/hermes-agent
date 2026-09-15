import { describe, expect, it } from 'vitest'

import type { CronJob } from '@/types/hermes'

import { sortCronJobsByName } from './cron-jobs-section'

const job = (id: string, name: string, nextRunAt?: string): CronJob => ({
  enabled: true,
  id,
  name,
  next_run_at: nextRunAt
})

describe('sortCronJobsByName', () => {
  it('orders jobs alphabetically instead of by next run time', () => {
    const jobs = [
      job('platform', 'Platform · Health', '2026-07-24T08:00:00Z'),
      job('briefing', 'Briefing · Daily AI digest', '2026-07-24T20:00:00Z'),
      job('business', 'Business · Daily schedule', '2026-07-24T12:00:00Z')
    ]

    expect(sortCronJobsByName(jobs).map(item => item.id)).toEqual(['briefing', 'business', 'platform'])
  })

  it('uses the job id as a deterministic tie-breaker for duplicate names', () => {
    const jobs = [job('b', 'Pipeline · Backup'), job('a', 'Pipeline · Backup')]

    expect(sortCronJobsByName(jobs).map(item => item.id)).toEqual(['a', 'b'])
  })

  it('does not mutate the backend-owned job list', () => {
    const jobs = [job('z', 'Zulu'), job('a', 'Alpha')]

    expect(sortCronJobsByName(jobs).map(item => item.id)).toEqual(['a', 'z'])
    expect(jobs.map(item => item.id)).toEqual(['z', 'a'])
  })
})
