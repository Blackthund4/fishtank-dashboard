import { Component } from 'react'

export default class ErrorBoundary extends Component {
  state = { hasError: false }
  static getDerivedStateFromError() { return { hasError: true } }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center h-screen bg-tank-bg text-tank-text font-mono gap-4">
          <div className="text-lg text-tank-danger">Something went wrong</div>
          <button onClick={() => { this.setState({ hasError: false }); window.location.reload() }}
            className="px-4 py-2 bg-tank-accent/10 border border-tank-accent/30 text-tank-accent rounded hover:bg-tank-accent/20">
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
