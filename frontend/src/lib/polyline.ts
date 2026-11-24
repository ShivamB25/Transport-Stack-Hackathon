/** Decodes a Google encoded polyline (precision 5) into [lat, lng] pairs; a truncated string yields the complete points. */
export function decodePolyline(encoded: string | null | undefined): [number, number][] {
  if (!encoded) return []
  const points: [number, number][] = []
  let index = 0
  let lat = 0
  let lng = 0
  while (index < encoded.length) {
    const deltas: number[] = []
    for (let axis = 0; axis < 2; axis++) {
      let shift = 0
      let result = 0
      let byte: number
      do {
        if (index >= encoded.length) return points
        byte = encoded.charCodeAt(index++) - 63
        result |= (byte & 0x1f) << shift
        shift += 5
      } while (byte >= 0x20)
      deltas.push(result & 1 ? ~(result >> 1) : result >> 1)
    }
    lat += deltas[0]
    lng += deltas[1]
    points.push([lat / 1e5, lng / 1e5])
  }
  return points
}
