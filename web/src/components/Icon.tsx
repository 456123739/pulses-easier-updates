interface Props {
  name: 'folder' | 'file' | 'chevron' | 'gear' | 'database' | 'list' | 'plus' | 'minus' | 'tilde' | 'shield' | 'sparkle'
  size?: number
  className?: string
}

const PATHS: Record<Props['name'], string> = {
  folder:
    'M3.5 6.2c0-1 .8-1.7 1.7-1.7h3l1.6 1.8h5.9c1 0 1.8.8 1.8 1.8v7.4c0 1-.8 1.8-1.8 1.8H5.2c-1 0-1.7-.8-1.7-1.7V6.2Z',
  file: 'M6 3.2h6.2L16.8 7v9.6c0 .9-.7 1.6-1.6 1.6H6c-.9 0-1.6-.7-1.6-1.6V4.8c0-.9.7-1.6 1.6-1.6Zm6 0V7h4.5',
  chevron: 'M8.2 5.6 13.4 10l-5.2 4.4',
  gear: 'M10 12.6a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Zm6.2-1.1.9 1.5-1.6 1.6-1.5-.9a6.4 6.4 0 0 1-1.6.6l-.4 1.7h-2.2l-.4-1.7a6.4 6.4 0 0 1-1.6-.6l-1.5.9-1.6-1.6.9-1.5a6.4 6.4 0 0 1-.6-1.6L3.3 9.4V7.2l1.7-.4c.1-.6.3-1.1.6-1.6L4.7 3.7 6.3 2.1l1.5.9c.5-.3 1-.5 1.6-.6l.4-1.7h2.2l.4 1.7c.6.1 1.1.3 1.6.6l1.5-.9 1.6 1.6-.9 1.5c.3.5.5 1 .6 1.6l1.7.4v2.2l-1.7.4c-.1.6-.3 1.1-.6 1.6Z',
  database:
    'M4 5.6c0-1.4 2.7-2.6 6-2.6s6 1.2 6 2.6-2.7 2.6-6 2.6-6-1.2-6-2.6Zm0 3.1c0 1.4 2.7 2.6 6 2.6s6-1.2 6-2.6M4 5.6v8.8c0 1.4 2.7 2.6 6 2.6s6-1.2 6-2.6V8.7',
  list: 'M6.5 6.5h9M6.5 10h9M6.5 13.5h9M4 6.5h.01M4 10h.01M4 13.5h.01',
  plus: 'M10 5v10M5 10h10',
  minus: 'M5 10h10',
  tilde: 'M5.5 11.5c1-2.6 2.2-3.9 3.6-3.9 1.9 0 2.4 3.4 4.3 3.4 1 0 1.8-.6 2.6-1.9',
  shield: 'M10 2.8 16 5v5.2c0 3.2-2.5 5.9-6 7-3.5-1.1-6-3.8-6-7V5l6-2.2Z',
  sparkle:
    'M10 3.2l1.6 4.2 4.2 1.6-4.2 1.6L10 14.8 8.4 10.6 4.2 9l4.2-1.6L10 3.2Z',
}

export default function Icon({ name, size = 14, className }: Props) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
