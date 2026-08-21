import { Button } from 'antd';
import { PlusOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';

interface Props {
  streakDays: number;
  onAdd: () => void;
  onSearch: () => void;
  onOpenSettings: () => void;
  onUploadResume?: () => void;
}

function greeting(): string {
  const h = dayjs().hour();
  if (h < 6) return '夜深了，注意休息';
  if (h < 12) return '早上好，继续加油';
  if (h < 18) return '下午好，保持节奏';
  return '晚上好，今天辛苦了';
}

export default function TopBar({
  streakDays,
  onAdd,
  onSearch,
  onOpenSettings,
}: Props) {
  return (
    <header
      className="op-topbar"
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '18px 24px',
      }}
    >
      <div>
        <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--op-ink)', letterSpacing: '-0.02em' }}>
          {greeting()} 👋
        </div>
        <div style={{ fontSize: 12, color: 'var(--op-muted)', marginTop: 2 }}>
          {dayjs().format('YYYY 年 M 月 D 日')}
          {streakDays > 0 && ` · 已连续投递 ${streakDays} 天`}
        </div>
      </div>
      <div className="op-topbar-actions" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button icon={<SearchOutlined />} onClick={onSearch}>
          快速打开 <span style={{ opacity: 0.6, marginLeft: 4 }}>⌘K</span>
        </Button>
        <Button icon={<SettingOutlined />} onClick={onOpenSettings} aria-label="设置" />
        <Button type="primary" icon={<PlusOutlined />} onClick={onAdd}>
          添加投递
        </Button>
      </div>
    </header>
  );
}
