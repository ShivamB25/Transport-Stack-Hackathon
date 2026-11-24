import { expect, test } from 'bun:test'
import { decodePolyline } from './polyline'

test('decodes the reference polyline from the Google encoding documentation', () => {
  expect(decodePolyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@')).toEqual([
    [38.5, -120.2],
    [40.7, -120.95],
    [43.252, -126.453],
  ])
})
