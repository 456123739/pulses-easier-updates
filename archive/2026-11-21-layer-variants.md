# 悬浮 3D 层的两套备选动效（已从主代码删除）

日期：2026-11-21
来源：`feat/sub-scope-layer` 分支，`web/src/components/Floating3DLayer.tsx`
说明：子项浮层最终只保留 **tilt**（-3° 落位、单层浅叠片）。
这里把 `stack` / `slide` 两套进场/退场参数留档，以后想换风格时直接贴回
`motionProps` 的对应分支即可。

## stack（-6° + 两层错位叠片）

```tsx
if (variant === 'stack') {
      return {
        initial: { opacity: 0, x: -20, rotateY: -20, rotateX: 5, scale: 0.92, filter: 'blur(12px)' },
        animate: { opacity: 1, x: 0, rotateY: -6, rotateX: 0, scale: 1, filter: 'blur(0px)' },
        exit: { opacity: 0, x: -12, rotateY: -12, scale: 0.97, filter: 'blur(8px)', transition: { duration: 0.16 } },
        transition: { type: 'spring' as const, stiffness: 240, damping: 22, mass: 0.8 },
      }
    }
```

CSS（叠片）：

```css
.plateNear { transform: translate3d(8px, 6px, -18px) scale(0.988) rotateY(-6deg); opacity: 0.7; }
.plateFar  { transform: translate3d(17px, 12px, -40px) scale(0.975) rotateY(-6deg); opacity: 0.45; }
```

## slide（不旋转，纯平移 + 模糊）

```tsx
if (variant === 'slide') {
```

## 还原方式

1. 在 `LayerVariant` 里加回 `'stack' | 'slide'`；
2. 把上面的分支贴回 `motionProps`；
3. `DeveloperView` 的 `?layer=stack|slide` 选择器恢复即可。
