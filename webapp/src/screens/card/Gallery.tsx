/** The pictures of a card's message: a strip of previews and a full-screen viewer (Back / tap closes it). */
import { useState } from "react";
import type { ImageRef } from "../../api/types";
import { AuthImage } from "../../components/AuthImage";
import { useBackHandler } from "../../lib/nav";

export function Gallery({ images, title }: { images: ImageRef[]; title: string }) {
  const [shown, setShown] = useState<ImageRef | null>(null);
  useBackHandler(shown !== null, () => setShown(null));
  if (!images.length) return null;
  return (
    <>
      <div className={images.length === 1 ? "gallery gallery--single" : "gallery"}>
        {images.map((image) => (
          <button
            key={image.n}
            type="button"
            className="gallery-item"
            onClick={() => setShown(image)}
            aria-label={`Открыть картинку ${image.n + 1} из ${images.length}`}
          >
            <AuthImage path={image.url} alt={`${title}: картинка ${image.n + 1}`} width={image.width} height={image.height} />
          </button>
        ))}
      </div>
      {shown && (
        <div className="viewer" role="dialog" aria-modal="true" aria-label="Картинка" onClick={() => setShown(null)}>
          <AuthImage path={shown.url} alt={`${title}: картинка ${shown.n + 1}`} lazy={false} className="viewer-image" />
          <button type="button" className="viewer-close" onClick={() => setShown(null)} aria-label="Закрыть">
            ✕
          </button>
        </div>
      )}
    </>
  );
}
