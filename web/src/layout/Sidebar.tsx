import {
  AppstoreOutlined,
  BulbOutlined,
  DashboardOutlined,
  FileTextOutlined,
  TrophyOutlined,
  RobotOutlined,
  SettingOutlined,
  AudioOutlined,
} from '@ant-design/icons';
import { Badge } from 'antd';
import { useThemeMode } from '@/theme/ThemeContext';
import {
  NAVIGATION_GROUPS,
  MODULE_NAV,
  resolveModuleForView,
  type ModuleKey,
  type ViewMode,
} from './navigation';
import styles from './Sidebar.module.css';

const MODULE_ICONS: Record<ModuleKey, React.ReactNode> = {
  today: <DashboardOutlined />,
  applications: <AppstoreOutlined />,
  interview: <AudioOutlined />,
  offers: <TrophyOutlined />,
  resources: <FileTextOutlined />,
  pilot: <RobotOutlined />,
  settings: <SettingOutlined />,
};

interface Props {
  view: ViewMode;
  onChange: (v: ViewMode) => void;
  reminderCount: number;
}

export default function Sidebar({ view, onChange, reminderCount }: Props) {
  const { mode, toggle } = useThemeMode();
  const activeModule = resolveModuleForView(view);
  const businessNav = MODULE_NAV.filter((item) => item.key !== 'settings');
  const settingsItem = MODULE_NAV.find((item) => item.key === 'settings');

  return (
    <nav
      className={`${styles.sidebar} op-sidebar`}
      aria-label="主导航"
    >
      <div className={styles.brand}>
        <span className={styles.brandMark} aria-hidden="true" />
        <span className={`${styles.brandName} op-gradient-text`}>
          OfferPilot
        </span>
      </div>

      {NAVIGATION_GROUPS.filter((group) => group.key !== 'utility').map((group) => {
        const items = businessNav.filter((item) => item.group === group.key);
        if (items.length === 0) return null;
        return (
          <section key={group.key} className={styles.group} aria-labelledby={`sidebar-group-${group.key}`}>
            <h2 id={`sidebar-group-${group.key}`} className={styles.groupLabel}>{group.label}</h2>
            {items.map((item) => {
              const active = activeModule === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-label={item.label}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => onChange(item.defaultView)}
                  className={`${styles.navItem} ${active ? styles.active : ''}`}
                >
                  <span className={styles.icon} aria-hidden="true">{MODULE_ICONS[item.key]}</span>
                  <span className={styles.label}>{item.label}</span>
                  {item.key === 'today' && reminderCount > 0 && (
                    <Badge count={reminderCount} size="small" />
                  )}
                </button>
              );
            })}
          </section>
        );
      })}

      <section className={styles.utility} aria-labelledby="sidebar-group-utility">
        <h2 id="sidebar-group-utility" className={styles.groupLabel}>辅助</h2>
        <button
          type="button"
          data-navigation-tier="utility"
          aria-label={settingsItem?.label ?? '设置'}
          aria-current={activeModule === 'settings' ? 'page' : undefined}
          onClick={() => onChange('settings')}
          className={`${styles.utilityButton} ${activeModule === 'settings' ? styles.active : ''}`}
        >
          <span className={styles.icon} aria-hidden="true"><SettingOutlined /></span>
          <span className={styles.label}>{settingsItem?.label ?? '设置'}</span>
        </button>
        <button
          type="button"
          onClick={toggle}
          aria-label="切换明暗模式"
          className={styles.utilityButton}
        >
          <span className={styles.icon} aria-hidden="true"><BulbOutlined /></span>
          <span className={styles.label}>{mode === 'dark' ? '亮色模式' : '暗色模式'}</span>
        </button>
      </section>
    </nav>
  );
}
