export type FastCommandParseResult =
  | { kind: "toggle" }
  | { kind: "set"; desiredActive: boolean }
  | { kind: "status" }
  | { kind: "usage" };

export function parseFastCommandArgs(args: string): FastCommandParseResult {
  switch (args.trim().toLowerCase()) {
    case "": return { kind: "toggle" };
    case "on": return { kind: "set", desiredActive: true };
    case "off": return { kind: "set", desiredActive: false };
    case "status": return { kind: "status" };
    default: return { kind: "usage" };
  }
}
