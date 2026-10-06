/** Who and where: GET /api/me, shared by every screen (the owner's timezone formats all dates). */
import { createContext, useContext } from "react";
import type { Me } from "../api/types";
import { KINDS, type KindInfo } from "./kinds";

export const DEFAULT_TZ = "Europe/Warsaw";

export interface MeState {
  me: Me | null;
  /** After the settings changed the timezone. */
  setTz: (tz: string) => void;
}

export const MeContext = createContext<MeState>({ me: null, setTz: () => undefined });

export function useMe(): Me | null {
  return useContext(MeContext).me;
}

/** The owner's timezone (IANA). */
export function useTz(): string {
  return useContext(MeContext).me?.tz ?? DEFAULT_TZ;
}

/** Categories with labels, in the bot's order. */
export function useKinds(): readonly KindInfo[] {
  const me = useMe();
  return me?.kinds.length ? me.kinds : KINDS;
}
