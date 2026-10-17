"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

interface RefreshContextValue {
    refreshEpoch: number;
    refreshNow: () => void;
}

const RefreshContext = createContext<RefreshContextValue | null>(null);

export function RefreshProvider({ children }: { children: ReactNode }) {
    const [refreshEpoch, setRefreshEpoch] = useState(0);
    const refreshNow = () => setRefreshEpoch((epoch) => epoch + 1);

    useEffect(() => {
        const interval = window.setInterval(refreshNow, 30_000);
        return () => window.clearInterval(interval);
    }, []);

    return (
        <RefreshContext.Provider value={{ refreshEpoch, refreshNow }}>
            {children}
        </RefreshContext.Provider>
    );
}

export function useRefresh(): RefreshContextValue {
    const value = useContext(RefreshContext);
    if (!value) throw new Error("useRefresh must be used within RefreshProvider");
    return value;
}