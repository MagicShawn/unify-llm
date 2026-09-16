/**
 * Global concurrency limiter.
 * acquire() throws { status: 429 } when at capacity; always pair with release().
 */
class ConcurrencyLimiter {
  constructor(max = 3) {
    this.max = Math.max(1, max | 0);
    this.active = 0;
  }

  get available() {
    return this.max - this.active;
  }

  acquire() {
    if (this.active >= this.max) {
      const err = new Error(`concurrency limit reached (${this.active}/${this.max})`);
      err.status = 429;
      err.retryAfter = 1;
      throw err;
    }
    this.active += 1;
    return this.active;
  }

  release() {
    if (this.active > 0) this.active -= 1;
    return this.active;
  }
}

module.exports = { ConcurrencyLimiter };
