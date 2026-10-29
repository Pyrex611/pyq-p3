import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import ModeSwitch from "./ModeSwitch";

/**
 * Shared page shell — topbar (brand + ModeSwitch) and primary nav.
 *
 * 2026-10-29 Next.js migration: this is the single shared layout the
 * owner's "Compare menu only shows on some pages" report (2026-10-28)
 * flagged a component-based frontend as the structural fix for — every
 * page previously copy-pasted this exact <nav> block into its own HTML
 * file (web/*.html), so a page that fell out of sync (or was added
 * later and missed the paste) would show a different nav than the
 * others. There is now exactly one copy of this markup; every page
 * renders it the same way by construction.
 */
const NAV = [
  ["/", "Dashboard"],
  ["/live-monitor", "Live Monitor"],
  ["/signals", "Signals"],
  ["/backtest", "Backtest"],
  ["/compare", "Compare"],
  ["/settings", "API Keys & Settings"],
];

export default function Layout({ title, children, footer }) {
  const router = useRouter();
  return (
    <>
      <Head>
        <title>{title ? `PyQuant — ${title}` : "PyQuant"}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </Head>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M3 17L9 11L13 15L21 7" stroke="#5B8DEF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M15 7H21V13" stroke="#5B8DEF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="name">PyQuant</span>
            <span className="subtitle">Control Center</span>
          </div>
          <div className="mode-switch">
            <ModeSwitch />
          </div>
        </header>
        <nav className="primary">
          {NAV.map(([href, label]) => (
            <Link key={href} href={href} className={router.pathname === href ? "active" : ""}>
              {label}
            </Link>
          ))}
        </nav>
        <main>{children}</main>
        {footer ? <footer className="hint">{footer}</footer> : null}
      </div>
    </>
  );
}
