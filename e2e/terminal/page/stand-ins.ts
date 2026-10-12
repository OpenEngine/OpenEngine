// What the page draws in place of IDs, timestamps and the ports of daemons on
// this machine, so a guide's screenshots are the same on every run: the daemon
// makes new IDs, the clock moves on and the OS picks a new port, but a reader
// needs none of them to be real. Each stand-in is as long as what it
// replaces, so nothing on screen moves, and an ID gets the same stand-in
// wherever it appears -- in output, or in a command a test typed. The buffer,
// and so every assertion, keeps the real values.

const HEX = "0123456789abcdef"

/** Engine IDs (g-…, gv-…, run-…) and UUIDs, as they appear in CLI output. */
const IDENTIFIER = /\b(?:[a-z]+-[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/g

/** ISO 8601 date-times, with or without seconds' fractions; the offset is kept as it is. */
const TIMESTAMP = /\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})?/g

/** Every timestamp shows this moment, cut or padded to the original's shape. */
const MOMENT = "2026-01-15T09:30:00.000000"

/**
 * A loopback host and a port. Only one the OS picked is replaced: one in the
 * ephemeral ranges, which start at 32768 on Linux and 49152 on macOS. A port
 * someone chose, like the daemon's own 4364, is shown as it is.
 */
const LOOPBACK_PORT = /(?<![\w.-])(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):(\d{5})(?!\d)/g

/** Where the ephemeral ranges start. */
const EPHEMERAL = 32768

/** The nth port on a page is drawn as 10001 + n: five digits, below every ephemeral range. */
const FIRST_PORT = 10001

export class StandIns {
  private readonly standIns = new Map<string, string>()
  private readonly used = new Set<string>()
  private readonly ports = new Map<string, string>()
  private seed = 0x2f6e2b1

  /** `text` with every ID, timestamp and loopback port replaced, or `text` itself if it has none. */
  apply(text: string): string {
    return text
      .replace(LOOPBACK_PORT, (address, host: string, port: string) =>
        Number(port) < EPHEMERAL ? address : `${host}:${this.portFor(port)}`,
      )
      .replace(IDENTIFIER, (id) => this.standInFor(id))
      .replace(
        TIMESTAMP,
        (_time, moment: string, offset?: string) =>
          moment.replace(/\d/g, (_digit, index: number) => MOMENT[index] ?? "0") + (offset ?? ""),
      )
  }

  private portFor(port: string): string {
    let standIn = this.ports.get(port)
    if (!standIn) {
      standIn = String(FIRST_PORT + this.ports.size)
      this.ports.set(port, standIn)
    }
    return standIn
  }

  private standInFor(id: string): string {
    if (this.used.has(id)) return id // already a stand-in
    let standIn = this.standIns.get(id)
    if (!standIn) {
      // An engine ID keeps its prefix (g-, run-, ...); a UUID is all digits.
      const [, prefix = "", digits = id] = /^([a-z]+-)([0-9a-f]{12})$/.exec(id) ?? []
      standIn = prefix + digits.replace(/[0-9a-f]/g, () => HEX[this.next() % 16]!)
      this.standIns.set(id, standIn)
      this.used.add(standIn)
    }
    return standIn
  }

  /** A fixed sequence (xorshift), so the nth new ID on a page always gets the same stand-in. */
  private next() {
    let x = this.seed
    x ^= x << 13
    x ^= x >>> 17
    x ^= x << 5
    this.seed = x >>> 0
    return this.seed
  }
}
