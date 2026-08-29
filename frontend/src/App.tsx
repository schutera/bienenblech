import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router-dom";
import { AuthProvider, RequireAdmin, RequireAuth, useAuth } from "./lib/auth";
import Picker from "./pages/Picker";
import Home from "./pages/Home";
import Label from "./pages/Label";
import AgeHome from "./pages/AgeHome";
import AgeLabel from "./pages/AgeLabel";
import Admin from "./pages/Admin";
import Login from "./pages/Login";

function navClass({ isActive }: { isActive: boolean }): string {
  return (
    "font-mono text-[11px] uppercase tracking-[0.16em] px-2 " +
    (isActive ? "text-accent" : "text-gray-tertiary hover:text-text")
  );
}

/** The app mark: a hexagon, drawn rather than an emoji. */
function Mark() {
  return (
    <div className="w-10 h-10 bg-accent grid place-items-center rounded" aria-hidden="true">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
        <path
          d="M12 2.5l8.2 4.75v9.5L12 21.5 3.8 16.75v-9.5z"
          stroke="white"
          strokeWidth="2"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

function UserMenu() {
  const { me, logout } = useAuth();
  if (!me) return null;
  return (
    <div className="flex items-center gap-2 ml-4 sm:ml-6">
      <span
        className="text-[13px] px-3 py-1.5 rounded-full border border-accent/40 bg-accent-soft text-accent-deep font-medium"
        title={`Signed in as ${me.username} (${me.role})`}
      >
        {me.username}
      </span>
      <button
        onClick={() => void logout()}
        className="text-[13px] px-3 py-1.5 rounded-full border border-border text-gray-mid hover:border-accent hover:text-accent-deep transition-colors"
      >
        Sign out
      </button>
    </div>
  );
}

/**
 * Which tool the current URL sits inside. The nav and the wordmark subtitle
 * hang off this: each tool shows only its own Overview/Label pair, and the
 * picker (or /admin, which is global) shows neither.
 */
function toolOf(pathname: string): "blech" | "age" | null {
  if (pathname === "/blech" || pathname.startsWith("/blech/")) return "blech";
  if (pathname === "/age" || pathname.startsWith("/age/")) return "age";
  return null;
}

/** /label/:cropId predates the picker; deep links into a crop stay alive. */
function LegacyLabelRedirect() {
  const { cropId } = useParams<{ cropId: string }>();
  return <Navigate to={cropId ? `/blech/label/${cropId}` : "/blech/label"} replace />;
}

/**
 * The signed-in shell. Its header height is mirrored by --app-header-h in
 * index.css (40px mark + py-4 + 1px border): the Blech Label screen parks the
 * crop progress bar — which carries the completeness rule — directly beneath
 * it, and that bar must never scroll out of view. Change one, change the other.
 */
function Shell() {
  const { isAdmin } = useAuth();
  const { pathname } = useLocation();
  const tool = toolOf(pathname);
  // Both Label routes are the exception to the centred column: the image under
  // judgment deserves the viewport, so they get full width and a much
  // shallower vertical rhythm.
  const labeling = pathname.startsWith("/blech/label") || pathname.startsWith("/age/label");
  // No top padding while labeling: Blech's crop-progress bar sticks flush
  // under the header, so nothing can scroll through a gap between the two.
  const mainCls = labeling
    ? "px-4 sm:px-6 pb-6"
    : "max-w-[1400px] mx-auto px-6 sm:px-10 py-10 sm:py-12";
  // The subtitle names the tool the user is inside; the picker and the global
  // /admin get the umbrella line. Blech's line stays free of bee vocabulary.
  const subtitle =
    tool === "blech"
      ? "polygon labeling for YOLO-seg"
      : tool === "age"
        ? "honeybee age annotation"
        : "annotation tools";

  return (
    <>
      <header className="sticky top-0 z-50 border-b border-border bg-bg">
        <div className="flex justify-between items-center px-6 sm:px-10 py-4 gap-4">
          <Link to="/" className="flex items-center gap-3">
            <Mark />
            <div>
              <div className="font-sans text-xl text-near-black leading-none">Bienenblech</div>
              <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-gray-tertiary mt-1">
                {subtitle}
              </div>
            </div>
          </Link>
          <nav className="flex items-center gap-2 sm:gap-4 flex-wrap justify-end">
            {tool ? (
              <>
                <NavLink to={`/${tool}`} end className={navClass}>
                  Overview
                </NavLink>
                <NavLink to={`/${tool}/label`} className={navClass}>
                  Label
                </NavLink>
              </>
            ) : null}
            {/* On the picker itself the header is just the brand and the user
                menu — the tiles are the navigation. */}
            {isAdmin && pathname !== "/" ? (
              <NavLink to="/admin" className={navClass}>
                Admin
              </NavLink>
            ) : null}
            <UserMenu />
          </nav>
        </div>
      </header>
      <main className={mainCls}>
        <Routes>
          <Route path="/" element={<Picker />} />
          {/* Blech — the original tool — moved under /blech when the picker
              took /. Age is its sibling. */}
          <Route path="/blech" element={<Home />} />
          <Route path="/blech/label" element={<Label />} />
          <Route path="/blech/label/:cropId" element={<Label />} />
          <Route path="/age" element={<AgeHome />} />
          <Route path="/age/label" element={<AgeLabel />} />
          {/* Old links stay alive: labeling moved under /blech, and the
              Images, Classes and Upload pages folded into Blech's Overview
              and Admin long ago — stale bookmarks land there, not on a 404. */}
          <Route path="/label" element={<Navigate to="/blech/label" replace />} />
          <Route path="/label/:cropId" element={<LegacyLabelRedirect />} />
          <Route path="/upload" element={<Navigate to="/blech" replace />} />
          <Route path="/images" element={<Navigate to="/blech" replace />} />
          <Route path="/classes" element={<Navigate to="/blech" replace />} />
          <Route path="/classes/*" element={<Navigate to="/blech" replace />} />
          <Route
            path="/admin"
            element={
              <RequireAdmin>
                <Admin />
              </RequireAdmin>
            }
          />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </>
  );
}

function NotFound() {
  return (
    <div className="max-w-xl">
      <h1 className="font-display text-3xl font-light text-near-black leading-tight">
        No such page
      </h1>
      <p className="text-[13px] text-gray-mid mt-2">
        The link is wrong or the page has moved. Use the navigation above.
      </p>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="*"
            element={
              <RequireAuth>
                <Shell />
              </RequireAuth>
            }
          />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
