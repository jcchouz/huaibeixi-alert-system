// Web Audio API Synthesized Industrial SHM Alarm Sound Engine
// Different acoustic signatures for Level 1 (Red Urgent), Level 2 (Orange Important), Level 3 (Yellow General)

const MUTE_STORAGE_KEY = 'shm_alert_sound_muted'

class AlertAudioEngine {
  constructor() {
    this.ctx = null
    this.muted = localStorage.getItem(MUTE_STORAGE_KEY) === 'true'
  }

  ensureContext() {
    if (typeof window === 'undefined') return null
    const AudioCtx = window.AudioContext || window.webkitAudioContext
    if (!AudioCtx) return null
    if (!this.ctx) {
      this.ctx = new AudioCtx()
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume().catch(() => {})
    }
    return this.ctx
  }

  isMuted() {
    return this.muted
  }

  setMuted(val) {
    this.muted = Boolean(val)
    localStorage.setItem(MUTE_STORAGE_KEY, String(this.muted))
    return this.muted
  }

  toggleMute() {
    return this.setMuted(!this.muted)
  }

  /**
   * Play synthesized industrial alarm tone according to alert_level (1, 2, 3)
   * @param {number} level - 1 (Red Urgent), 2 (Orange Important), 3 (Yellow General)
   * @param {boolean} force - Play even if muted (for manual test button when user explicitly tests audio)
   */
  playAlertBeep(level = 1, force = false) {
    if (this.muted && !force) return
    try {
      const ctx = this.ensureContext()
      if (!ctx) return

      const now = ctx.currentTime

      if (Number(level) === 1) {
        // Level 1 Red Urgent: High-priority 3-pulse dual-frequency industrial siren (880Hz <-> 1174Hz)
        const pulses = [
          { start: 0.0, dur: 0.14, freqA: 880, freqB: 1180 },
          { start: 0.18, dur: 0.14, freqA: 880, freqB: 1180 },
          { start: 0.36, dur: 0.24, freqA: 980, freqB: 1320 }
        ]
        pulses.forEach((p) => {
          const osc = ctx.createOscillator()
          const gain = ctx.createGain()
          osc.type = 'sawtooth'
          osc.frequency.setValueAtTime(p.freqA, now + p.start)
          osc.frequency.linearRampToValueAtTime(p.freqB, now + p.start + p.dur)

          gain.gain.setValueAtTime(0.001, now + p.start)
          gain.gain.exponentialRampToValueAtTime(0.22, now + p.start + 0.02)
          gain.gain.exponentialRampToValueAtTime(0.001, now + p.start + p.dur)

          osc.connect(gain)
          gain.connect(ctx.destination)
          osc.start(now + p.start)
          osc.stop(now + p.start + p.dur + 0.01)
        })
      } else if (Number(level) === 2) {
        // Level 2 Orange Important: Medium-priority double-pulse warning tone (660Hz -> 784Hz)
        const pulses = [
          { start: 0.0, dur: 0.16, freq: 659.25 },
          { start: 0.22, dur: 0.20, freq: 783.99 }
        ]
        pulses.forEach((p) => {
          const osc = ctx.createOscillator()
          const gain = ctx.createGain()
          osc.type = 'triangle'
          osc.frequency.setValueAtTime(p.freq, now + p.start)

          gain.gain.setValueAtTime(0.001, now + p.start)
          gain.gain.exponentialRampToValueAtTime(0.18, now + p.start + 0.02)
          gain.gain.exponentialRampToValueAtTime(0.001, now + p.start + p.dur)

          osc.connect(gain)
          gain.connect(ctx.destination)
          osc.start(now + p.start)
          osc.stop(now + p.start + p.dur + 0.01)
        })
      } else {
        // Level 3 Yellow General: Crisp single sonar chime (523.25Hz C5)
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        osc.type = 'sine'
        osc.frequency.setValueAtTime(523.25, now)
        osc.frequency.exponentialRampToValueAtTime(659.25, now + 0.25)

        gain.gain.setValueAtTime(0.001, now)
        gain.gain.exponentialRampToValueAtTime(0.15, now + 0.02)
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.28)

        osc.connect(gain)
        gain.connect(ctx.destination)
        osc.start(now)
        osc.stop(now + 0.3)
      }
    } catch (err) {
      console.warn('Web Audio API playback skipped:', err)
    }
  }
}

export const alertAudio = new AlertAudioEngine()
