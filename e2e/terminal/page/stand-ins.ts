// What the page draws in place of IDs, timestamps and ports the OS picked on
// this computer, so a guide's screenshots are the same on every run: the
// daemon makes new IDs, the clock moves on and a test daemon listens wherever
// the OS put it, but a reader needs none of them to be real. Each stand-in is as long as what it
// replaces, so nothing on screen moves, and an ID gets the same stand-in
// wherever it appears -- in output, or in a command a test typed. The buffer,
// and so every assertion, keeps the real values.

const HEX = "0123456789abcdef"

/** Engine IDs (g-…, gv-…, run-…) and UUIDs, as they appear in CLI output. */
const IDENTIFIER = /\b(?:[a-z]+-[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/g

/** ISO 8601 date-times, with or without seconds' fractions (after a comma in Python's logs); the offset is kept as it is. */
const TIMESTAMP = /\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)(Z|[+-]\d{2}:\d{2})?/g

/** A port on this computer's loopback address, where a test daemon listens on a port the OS picked. */
const LOOPBACK_PORT = /(?<![\w.])((?:127\.0\.0\.1|localhost|\[::1\]):)(\d{2,5})\b/g

/** The random end of a test daemon's temporary directory name, which the daemon prints when it starts. */
const TEMPORARY_ROOT = /(?<=\bopenengine-e2e-)(?!bin-)[A-Za-z0-9]{6}\b/g

/** The process ID a daemon reports when it starts; one stand-in whatever its length, since nothing follows it on the line. */
const SERVER_PROCESS = /(?<=\bStarted server process \[)\d+(?=\])/g

/** What precedes that process ID when it is drawn in a colour of its own, and so in a text node of its own. */
const BEFORE_SERVER_PROCESS = /\bStarted server process \[$/

/** The first port drawn in their place; the nth new one of a length gets the nth after it, cut to that length. */
const FIRST_PORT = 54321

/** Every timestamp shows this moment, cut or padded to the original's shape. */
const MOMENT = "2026-01-15T09:30:00.000000"

export class StandIns {
  private readonly standIns = new Map<string, string>()
  private readonly used = new Set<string>()
  private readonly ports = new Map<string, string>()
  private readonly tunnelAddress?: string

  constructor(tunnelAddress?: string) { this.tunnelAddress = tunnelAddress }

  private seed = 0x2f6e2b1

  /**
   * `text` with every ID, timestamp, loopback port, temporary directory and daemon process ID replaced, or `text` itself if it has none.
   * `before` is the text drawn just before it, for a value coloured on its own.
   */
  apply(text: string, before = ""): string {
    if (/^\d+$/.test(text) && BEFORE_SERVER_PROCESS.test(before)) return "4242"
    if (this.tunnelAddress) {
      text = text
        .replace(/\b[a-z0-9-]+\.trycloudflare\.com\b/g, this.tunnelAddress)
        .replace(/\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b/g, this.tunnelAddress)
    }
    return text
      .replace(TEMPORARY_ROOT, "Xq3sT7")
      .replace(SERVER_PROCESS, "4242")
      .replace(IDENTIFIER, (id) => this.standInFor(id))
      .replace(LOOPBACK_PORT, (_address, host: string, port: string) => host + this.portFor(port))
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

  private portFor(port: string): string {
    if ([...this.ports.values()].includes(port)) return port // already a stand-in
    let standIn = this.ports.get(port)
    if (!standIn) {
      // Counted per length, so the shorter ports a command shows while it is
      // being typed don't move a whole one's stand-in. Distinct for every new
      // port of one length; ports of different lengths can't share one.
      const before = [...this.ports.keys()].filter((seen) => seen.length === port.length).length
      standIn = String(FIRST_PORT + before).slice(-port.length).padStart(port.length, "0")
      this.ports.set(port, standIn)
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
