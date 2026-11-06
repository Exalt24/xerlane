import { defineConfig } from 'vitest/config'

// The tests run against a real PostgreSQL (python tools/pg.py up) in a database of their own, created and dropped by test/global-setup.ts, and the API tests
// talk to the built server (npm run build first). A missing database or a missing build fails the run; nothing is skipped.
export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    globalSetup: ['test/global-setup.ts'],
    testTimeout: 30000,
    hookTimeout: 120000,
    fileParallelism: false,
  },
})
