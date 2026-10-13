// What the page draws in place of IDs, timestamps and OS-picked ports, so a guide's screenshots
// are the same on every run: the daemon makes new IDs, the clock moves on and
// each e2e daemon listens on a port the OS picked, but a reader needs none of
// them to be real. Each stand-in is as long as what it
// replaces, so nothing on screen moves, and an ID gets the same stand-in
// wherever it appears -- in output, or in a command a test typed. The buffer,
// and so every assertion, keeps the real values.

const HEX = "0123456789abcdef"

/** Engine IDs (g-…, gv-…, run-…) and UUIDs, as they appear in CLI output. */
const IDENTIFIER = /\b(?:[a-z]+-[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/g

/** ISO 8601 date-times, with or without seconds' fractions; the offset is kept as it is. */
const TIMESTAMP = /\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})?/g

/**
 * A port on this machine in the range operating systems pick from (Linux from
 * 32768, macOS from 49152). A port someone chose, like 4364, is shown as it is.
 */
const LOOPBACK_PORT = /(?<![\w.])(127\.0\.0\.1|localhost|\[::1\]):(\d{5})\b/g
const FIRST_PICKED_PORT = 32768

/** Port stand-ins count up from here: five digits, like the ports they replace, and never one the OS picks. */
const FIRST_PORT_STAND_IN = 18001

/** Every timestamp shows this moment, cut or padded to the original's shape. */
const MOMENT = "2026-01-15T09:30:00.000000"

export class StandIns {
  private readonly standIns = new Map<string, string>()
  private readonly used = new Set<string>()
  private readonly portStandIns = new Map<string, string>()
  private seed = 0x2f6e2b1

  /** `text` with every ID, timestamp and picked port replaced, or `text` itself if it has none. */
  apply(text: string): string {
    return text
      .replace(LOOPBACK_PORT, (address, host: string, port: string) =>
        Number(port) < FIRST_PICKED_PORT ? address : `${host}:${this.portStandInFor(port)}`,
      )
      .replace(IDENTIFIER, (id) => this.standInFor(id))
      .replace(
        TIMESTAMP,
        (_time, moment: string, offset?: string) =>
          moment.replace(/\d/g, (_digit, index: number) => MOMENT[index] ?? "0") + (offset ?? ""),
      )
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

  /** The nth picked port on a page always gets the same stand-in, below the picked range so it is never replaced again. */
  private portStandInFor(port: string): string {
    let standIn = this.portStandIns.get(port)
    if (!standIn) {
      standIn = String(FIRST_PORT_STAND_IN + this.portStandIns.size)
      this.portStandIns.set(port, standIn)
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
