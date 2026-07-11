"use client";

import type { KeyboardEvent } from "react";

export interface TabItem<T extends string> {
  id: T;
  label: string;
  description: string;
}

interface TabNavigationProps<T extends string> {
  activeId: T;
  ariaLabel: string;
  idPrefix: string;
  items: readonly TabItem<T>[];
  onChange: (id: T) => void;
}

/** Accessible tab navigation shared by the dashboard and Settings workspace. */
export function TabNavigation<T extends string>({
  activeId,
  ariaLabel,
  idPrefix,
  items,
  onChange,
}: TabNavigationProps<T>) {
  const selectTab = (id: T) => {
    onChange(id);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, currentId: T) => {
    const currentIndex = items.findIndex((item) => item.id === currentId);
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex == null) return;

    event.preventDefault();
    const nextId = items[nextIndex].id;
    selectTab(nextId);
    document.getElementById(`${idPrefix}-tab-${nextId}`)?.focus();
  };

  return (
    <nav className="tab-navigation" aria-label={ariaLabel} role="tablist">
      {items.map((item) => (
        <button
          key={item.id}
          id={`${idPrefix}-tab-${item.id}`}
          className={`tab-navigation-item ${activeId === item.id ? "active" : ""}`}
          role="tab"
          aria-controls={`${idPrefix}-panel-${item.id}`}
          aria-selected={activeId === item.id}
          onClick={() => selectTab(item.id)}
          onKeyDown={(event) => handleKeyDown(event, item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}
