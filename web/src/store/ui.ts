import { create } from 'zustand'

export type SheetKind =
  | 'preferences'
  | 'pending'
  | 'changelog'
  | 'db'
  | 'whitelist'
  | null

interface UiStore {
  sheet: SheetKind
  openSheet: (s: SheetKind) => void
  closeSheet: () => void
  covering: boolean
  setCovering: (v: boolean) => void
}

export const useUi = create<UiStore>((set) => ({
  sheet: null,
  openSheet: (sheet) => set({ sheet }),
  closeSheet: () => set({ sheet: null }),
  covering: false,
  setCovering: (covering) => set({ covering }),
}))
