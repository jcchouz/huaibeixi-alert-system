export function formatDateTimeWithMs(value) {
  if (value === null || value === undefined || value === '') return '-'
  const text = String(value).trim()
  if (!text) return '-'

  const match = text.match(
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?/
  )
  if (!match) return text

  const milliseconds = (match[3] || '').padEnd(3, '0').slice(0, 3)
  return `${match[1]} ${match[2]}.${milliseconds}`
}
