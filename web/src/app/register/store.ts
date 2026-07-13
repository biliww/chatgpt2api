"use client";

import { create } from "zustand";
import { toast } from "sonner";

import {
  fetchRegisterConfig,
  updateRegisterConfig,
  startRegister,
  stopRegister,
  resetRegister,
  type RegisterConfig,
} from "@/lib/register-api";

const MAX_LOGS = 500;

type RegisterStore = {
  registerConfig: RegisterConfig | null;
  isLoadingRegister: boolean;
  isSavingRegister: boolean;

  loadRegister: () => Promise<void>;
  setRegisterConfig: (data: RegisterConfig) => void;

  setRegisterProxy: (value: string) => void;
  setRegisterTotal: (value: string) => void;
  setRegisterThreads: (value: string) => void;
  setRegisterMode: (value: RegisterConfig["mode"]) => void;
  setRegisterTargetQuota: (value: string) => void;
  setRegisterTargetAvailable: (value: string) => void;
  setRegisterCheckInterval: (value: string) => void;
  setRegisterMailField: (key: string, value: string) => void;
  addRegisterProvider: () => void;
  updateRegisterProvider: (index: number, data: Record<string, unknown>) => void;
  deleteRegisterProvider: (index: number) => void;

  saveRegister: () => Promise<void>;
  toggleRegister: () => Promise<void>;
  resetRegister: () => Promise<void>;
};

export const useRegisterStore = create<RegisterStore>((set, get) => ({
  registerConfig: null,
  isLoadingRegister: false,
  isSavingRegister: false,

  loadRegister: async () => {
    set({ isLoadingRegister: true });
    try {
      const data = await fetchRegisterConfig();
      set({ registerConfig: data.register });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册配置失败");
    } finally {
      set({ isLoadingRegister: false });
    }
  },

  setRegisterConfig: (data) => set({ registerConfig: data }),

  setRegisterProxy: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, proxy: value } : s.registerConfig,
    }));
  },
  setRegisterTotal: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, total: Math.max(1, Number(value) || 1) } : s.registerConfig,
    }));
  },
  setRegisterThreads: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, threads: Math.max(1, Number(value) || 1) } : s.registerConfig,
    }));
  },
  setRegisterMode: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, mode: value } : s.registerConfig,
    }));
  },
  setRegisterTargetQuota: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, target_quota: Math.max(1, Number(value) || 1) } : s.registerConfig,
    }));
  },
  setRegisterTargetAvailable: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, target_available: Math.max(1, Number(value) || 1) } : s.registerConfig,
    }));
  },
  setRegisterCheckInterval: (value) => {
    set((s) => ({
      registerConfig: s.registerConfig ? { ...s.registerConfig, check_interval: Math.max(1, Number(value) || 5) } : s.registerConfig,
    }));
  },
  setRegisterMailField: (key, value) => {
    set((s) => {
      if (!s.registerConfig) return {};
      const existing = s.registerConfig.mail;
      const numeric = Number(value);
      const mail = { ...existing, [key]: isNaN(numeric) ? existing[key as keyof typeof existing] : numeric };
      return { registerConfig: { ...s.registerConfig, mail } };
    });
  },

  addRegisterProvider: () => {
    set((s) => {
      if (!s.registerConfig) return {};
      const providers = [...s.registerConfig.mail.providers, { type: "tempmail_lol", enable: true }];
      return { registerConfig: { ...s.registerConfig, mail: { ...s.registerConfig.mail, providers } } };
    });
  },
  updateRegisterProvider: (index, data) => {
    set((s) => {
      if (!s.registerConfig) return {};
      const providers = s.registerConfig.mail.providers.map((p, i) =>
        i === index ? { ...p, ...data } : p
      );
      return { registerConfig: { ...s.registerConfig, mail: { ...s.registerConfig.mail, providers } } };
    });
  },
  deleteRegisterProvider: (index) => {
    set((s) => {
      if (!s.registerConfig) return {};
      const providers = s.registerConfig.mail.providers.filter((_, i) => i !== index);
      return { registerConfig: { ...s.registerConfig, mail: { ...s.registerConfig.mail, providers } } };
    });
  },

  saveRegister: async () => {
    const cfg = get().registerConfig;
    if (!cfg) {
      toast.error("暂无配置可保存");
      return;
    }
    set({ isSavingRegister: true });
    try {
      const data = await updateRegisterConfig(cfg);
      set({ registerConfig: data.register, isSavingRegister: false });
      toast.success("配置已保存");
    } catch (error) {
      set({ isSavingRegister: false });
      toast.error(error instanceof Error ? error.message : "保存失败");
    }
  },

  toggleRegister: async () => {
    const cfg = get().registerConfig;
    if (!cfg) return;
    try {
      if (cfg.enabled) {
        const data = await stopRegister();
        set({ registerConfig: data.register });
        toast.success("已停止");
      } else {
        const data = await startRegister();
        set({ registerConfig: data.register });
        toast.success("注册任务已启动");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "操作失败");
    }
  },

  resetRegister: async () => {
    try {
      const data = await resetRegister();
      set({ registerConfig: data.register });
      toast.success("统计已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置失败");
    }
  },
}));
