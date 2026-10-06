/** A picture preview loaded with the API's authorization (blob URL), lazily when it scrolls into view. */
import { useEffect, useRef, useState } from "react";
import { useImage } from "../lib/images";

function useVisible<T extends Element>(lazy: boolean) {
  const ref = useRef<T>(null);
  const [visible, setVisible] = useState(!lazy || typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (visible || !ref.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [visible]);
  return { ref, visible };
}

export function AuthImage({
  path,
  alt,
  className,
  lazy = true,
  width,
  height,
}: {
  path: string;
  alt: string;
  className?: string;
  lazy?: boolean;
  width?: number | null;
  height?: number | null;
}) {
  const { ref, visible } = useVisible<HTMLSpanElement>(lazy);
  const image = useImage(path, visible);
  const style = width && height ? { aspectRatio: `${width} / ${height}` } : undefined;
  return (
    <span ref={ref} className={`auth-image ${className ?? ""}`} style={style}>
      {image.url ? (
        <img src={image.url} alt={alt} decoding="async" onError={image.retry} />
      ) : image.failed ? (
        <span className="auth-image-failed" role="img" aria-label="Картинка не загрузилась">
          🖼
        </span>
      ) : (
        <span className="skeleton auth-image-wait" aria-hidden="true" />
      )}
    </span>
  );
}
