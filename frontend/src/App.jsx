import { useState, useRef, useEffect } from 'react'
import './App.css'

export default function App() {
  const [messages, setMessages] = useState([
    { role: 'system', text: 'Hi! Ask me anything about the reference material.' },
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
        { role: 'system', text: 'Something went wrong reaching the server. Please try again.' },
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

  function openDoc(url) {
    window.open(url, '_blank', 'noopener,noreferrer')
  }

  return (
    <div className="shell">
      <div className="frame">
        <header className="header">
          <h1 className="wordmark">vaakbot</h1>
          <p className="tagline">
            Ask about the reference material - I will answer from what I know,
            and say so plainly if I do not.
          </p>
        </header>

        <div className="messages" ref={scrollRef}>
          {messages.map((m, i) => {
            const sources = m.sources || []
            return (
              <div key={i} className={`bubble ${m.role}`}>
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
                              onClick={() => openDoc(s.doc_url)}
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
            )
          })}
          {sending && (
            <div className="wave-indicator" aria-label="vaakbot is thinking">
              <span className="bar" />
              <span className="bar" />
              <span className="bar" />
              <span className="bar" />
              <span className="bar" />
            </div>
          )}
        </div>

        <div className="input-row">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type a question..."
            autoComplete="off"
            disabled={sending}
          />
          <button onClick={sendMessage} disabled={sending || !input.trim()}>
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
