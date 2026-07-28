import { Activity, LibraryBig, Plus, Settings2, Sparkles } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import styles from "./AppShell.module.css";

const navigation = [
  { to: "/", label: "Tạo mới", icon: Plus, end: true },
  { to: "/library", label: "Thư viện", icon: LibraryBig },
  { to: "/system", label: "Hệ thống", icon: Settings2 },
];

export function AppShell() {
  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <NavLink to="/" className={styles.brand} aria-label="YT Pro Max">
          <span className={styles.brandMark}><Sparkles size={19} /></span>
          <span><strong>YT Pro Max</strong><small>Editorial engine</small></span>
        </NavLink>
        <nav className={styles.navigation} aria-label="Điều hướng chính">
          {navigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => `${styles.navItem} ${isActive ? styles.active : ""}`}>
              <Icon size={19} strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className={styles.sidebarNote}>
          <Activity size={16} />
          <p>Mọi tác vụ tiếp tục chạy trên máy ngay cả khi bạn đóng tab.</p>
        </div>
      </aside>
      <main className={styles.main}>
        <Outlet />
      </main>
      <nav className={styles.mobileNav} aria-label="Điều hướng di động">
        {navigation.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => `${styles.mobileNavItem} ${isActive ? styles.active : ""}`}>
            <Icon size={19} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
