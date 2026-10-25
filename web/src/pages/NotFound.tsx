import { Link } from "react-router";

export function NotFound() {
  return (
    <div className="page">
      <div className="empty">
        <h1 className="display">This page doesn't exist</h1>
        <p className="muted">The link may be out of date, or the item was deleted.</p>
        <Link className="btn btn-primary" to="/systems">
          Go to your systems
        </Link>
      </div>
    </div>
  );
}
