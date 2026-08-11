/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ErrorBoundary — catches render-time exceptions in child trees and
 * shows a recoverable fallback instead of a blank white panel.
 *
 * Why: the LmStudioSection's useBlocker call previously threw because
 * the app uses a declarative BrowserRouter; without a boundary the
 * entire Settings shell rendered blank with no error message and no
 * recovery path. Wrap every top-level route in this so future
 * component-level throws stay scoped + show actionable UI.
 */
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import "@/styles/error-boundary.css";

interface Props {
  children: ReactNode;
  /** Optional surface name surfaced in the fallback copy. */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    if (this.state.error !== null) {
      const err = this.state.error;
      return (
        <div
          className="eb-surface"
          role="alert"
          data-testid="error-boundary-fallback"
        >
          <h2 className="eb-title">Something interrupted us.</h2>
          <p className="eb-description">
            An unexpected error occurred
            {this.props.label ? ` in ${this.props.label}` : ""}. You can try
            again or return to chat.
          </p>
          <div className="eb-actions">
            <button
              type="button"
              onClick={this.reset}
              className="eb-btn-reload"
            >
              Try again
            </button>
            <button
              type="button"
              onClick={() => {
                window.location.assign("/");
              }}
              className="eb-btn-back"
            >
              Back to chat
            </button>
          </div>
          <details className="eb-details">
            <summary className="eb-details-summary">Developer details</summary>
            <pre className="eb-stack">
              {err.message}
              {err.stack ? `\n\n${err.stack}` : ""}
            </pre>
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}
