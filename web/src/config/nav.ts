export type OptionalNavItem = {
  href: string;
  label: string;
  roles?: string[];
};

// 可选 / 本地功能的导航项集中在此（扩展点）。
// 新增本地入口只改这里，不再改动 top-nav.tsx 的 adminNavItems 本体，
// 从而保证 merge main 友好。
export const optionalNavItems: OptionalNavItem[] = [
  { href: "/register", label: "注册机", roles: ["admin"] },
];
