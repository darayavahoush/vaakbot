import { useState, useRef, useEffect } from 'react'
import './App.css'

function Milo({ size = 40, thinking = false }) {
  return (
    <svg
      className={`milo${thinking ? ' milo-thinking' : ''}`}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      aria-hidden="true"
    >
      <ellipse className="milo-wing" cx="20" cy="62" rx="12" ry="20" />
      <ellipse className="milo-wing" cx="80" cy="62" rx="12" ry="20" />
      <path className="milo-tuft" d="M32 22 L28 8 L40 18 Z" />
      <path className="milo-tuft" d="M68 22 L72 8 L60 18 Z" />
      <ellipse className="milo-body" cx="50" cy="58" rx="34" ry="30" />
      <ellipse className="milo-chest" cx="50" cy="66" rx="19" ry="16" />
      <ellipse className="milo-face" cx="50" cy="44" rx="27" ry="23" />
      <g className="milo-eye-l">
        <circle cx="38" cy="42" r="9" className="milo-eye-white" />
        <circle cx="38" cy="42" r="4" className="milo-pupil" />
      </g>
      <g className="milo-eye-r">
        <circle cx="62" cy="42" r="9" className="milo-eye-white" />
        <circle cx="62" cy="42" r="4" className="milo-pupil" />
      </g>
      <path className="milo-beak" d="M50 50 L45 58 L55 58 Z" />
      <ellipse className="milo-foot" cx="42" cy="90" rx="6" ry="4" />
      <ellipse className="milo-foot" cx="58" cy="90" rx="6" ry="4" />
    </svg>
  )
}

export default function App() {
  const [messages, setMessages] = useState([
    {
      role: 'system',
      text: "Hi, I'm Milo. Ask me anything about the reference material — I'll answer from what I know, and say so plainly if I don't.",
    },
  ])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sessionId, setSessionId] = useState(null)
  const [expandedSource, setExpandedSource] = useState(null)
  const scrollRef = useRef(null)

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, sending])

  async function sendMessage() {
    const text = input.trim()
    if (!text || sending) return

    setMessages((m) => [...m, { role: 'user', text }])
    setInput('')
    setSending(true)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      })
      const data = await res.json()
      setSessionId(data.session_id)
      setMessages((m) => [
        ...m,
        {
          role: data.restarted ? 'system' : 'bot',
          text: data.reply,
          sources: data.sources || [],
        },
      ])
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: 'system', text: "Milo couldn't reach the server. Please try again." },
      ])
    } finally {
      setSending(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter') sendMessage()
  }

  function toggleSource(key) {
    setExpandedSource((current) => (current === key ? null : key))
  }

  function openDoc(source, snippet) {
    const url =
      '/api/docs/view/' + encodeURIComponent(source) +
      '?q=' + encodeURIComponent(snippet || '')
    window.open(url, '_blank', 'noopener,noreferrer')
  }

  return (
    <div className="shell">
      <div className="frame">
        <header className="header">
          <div className="header-avatar">
            <div className="avatar-blob" />
            <Milo size={52} />
          </div>
          <div className="header-text">
            <h1 className="wordmark">Milo</h1>
            <p className="tagline">
              Your guide to the reference material. Ask a question and I'll
              answer from what I've been given — nothing more.
            </p>
          </div>
        </header>

        <div className="messages" ref={scrollRef}>
          {messages.map((m, i) => {
            const sources = m.sources || []
            return (
              <div key={i} className={`row row-${m.role}`}>
                {m.role === 'bot' && (
                  <div className="avatar-small">
                    <Milo size={30} />
                  </div>
                )}
                <div className={`bubble ${m.role}`}>
                  {m.text}
                  {sources.length > 0 && (
                    <div className="sources">
                      {sources.map((s, j) => {
                        const key = i + '-' + j
                        const isOpen = expandedSource === key
                        return (
                          <div key={key} className="source-item">
                            <div className="source-header">
                              <span className="source-name">{s.source}</span>
                              <button
                                type="button"
                                className="link-btn"
                                onClick={() => toggleSource(key)}
                              >
                                {isOpen ? 'Hide' : 'Read more'}
                              </button>
                              <button
                                type="button"
                                className="link-btn"
                                onClick={() => openDoc(s.source, s.snippet)}
                              >
                                View full doc
                              </button>
                            </div>
                            {isOpen && (
                              <p className="source-snippet">{s.snippet}</p>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {sending && (
            <div className="row row-bot">
              <div className="avatar-small">
                <Milo size={30} thinking />
              </div>
              <div className="thinking-note">Milo is thinking...</div>
            </div>
          )}
        </div>

        <div className="input-row">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask Milo a question..."
            autoComplete="off"
            disabled={sending}
          />
          <button onClick={sendMessage} disabled={sending || !input.trim()}>
            Ask
          </button>
        </div>
      </div>
    </div>
  )
}
