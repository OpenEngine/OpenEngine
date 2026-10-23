// The other computer lives here so that the guide and the specs that check
// what it shows can't drift apart on where it answers.

/** Where the Mac mini in the other-computer guide answers. */
export const MAC_MINI = "mac-mini.local:4364"

/** MAC_MINI escaped for use inside a RegExp. */
export const MAC_MINI_PATTERN = MAC_MINI.replace(/\./g, "\\.")
