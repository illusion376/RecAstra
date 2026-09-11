import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: 'RecAstra — требования из встреч',
  description: 'Рабочее пространство для анализа встреч, проверки требований и подготовки технического задания.',
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="ru"><body>{children}</body></html>;
}
