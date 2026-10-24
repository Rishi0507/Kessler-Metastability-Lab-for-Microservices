export class RetryBudget {
  private tokens: number;
  private last = Date.now();

  constructor(
    private readonly ratio = 0.1,
    private readonly minPerSec = 1,
    private readonly max = 10,
  ) {
    this.tokens = max / 2;
  }

  private refill(): void {
    const now = Date.now();
    this.tokens = Math.min(this.max, this.tokens + ((now - this.last) / 1000) * this.minPerSec);
    this.last = now;
  }

  deposit(): void {
    this.refill();
    this.tokens = Math.min(this.max, this.tokens + this.ratio);
  }

  withdraw(): boolean {
    this.refill();
    if (this.tokens >= 1) {
      this.tokens -= 1;
      return true;
    }
    return false;
  }
}

export function backoffMs(attempt: number, baseMs = 200, capMs = 4000, rand: () => number = Math.random): number {
  const ceiling = Math.min(capMs, baseMs * 2 ** attempt);
  return Math.floor(rand() * ceiling);
}

export const sleep = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
